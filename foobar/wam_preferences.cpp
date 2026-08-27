#include <windows.h>
#include <mmsystem.h>
#include <objidl.h>

#include <foobar2000/SDK/foobar2000.h>
#include <foobar2000/SDK/coreDarkMode.h>

#include "wam_settings.h"

#include <algorithm>
#include <array>
#include <cwchar>
#include <string>
#include <utility>

namespace {

constexpr wchar_t kSection[] = L"wambridge";
constexpr wchar_t kWindowClass[] = L"WAMBridgePreferencesPage";
using wam_settings::kDefaultBufferExtraMs;
using wam_settings::kDefaultSleepAfterStopSeconds;
using wam_settings::kDefaultStartVolumeMax;
using wam_settings::kDefaultStartupSilenceMs;
using wam_settings::kDefaultVolumeMax;
using wam_settings::kMaximumBufferExtraMs;
using wam_settings::kMaximumRawVolume;
using wam_settings::kMaximumSleepAfterStopSeconds;
using wam_settings::kMaximumStartupSilenceMs;
constexpr int kRowBase = 80;
constexpr int kRowPitch = 27;

// {D50FA20F-41FC-4F2C-B76B-3C65DD129028}
constexpr GUID kPreferencesGuid = {
    0xd50fa20f,
    0x41fc,
    0x4f2c,
    {0xb7, 0x6b, 0x3c, 0x65, 0xdd, 0x12, 0x90, 0x28},
};

enum ControlId {
    kDevice = 1001,
    kFormat,
    kVolume,
    kHardwareVolume,
    kVolumeMax,
    kStartVolumeMax,
    kStartupSilence,
    kBufferExtra,
    kSleepAfterStop,
    kDiagnostics,
    kHelper,
};

using wam_settings::Values;
using wam_settings::clamped_int;
using wam_settings::config_path;
using wam_settings::default_values;
using wam_settings::equal_values;
using wam_settings::has_environment_overrides;
using wam_settings::write_values;

std::wstring window_text(HWND window) {
    const int length = GetWindowTextLengthW(window);
    if (length <= 0) return {};
    std::wstring text(static_cast<size_t>(length) + 1, L'\0');
    const int written = GetWindowTextW(window, text.data(), length + 1);
    if (written <= 0) return {};
    text.resize(static_cast<size_t>(written));
    return text;
}

class WamPreferencesInstance : public preferences_page_instance {
public:
    WamPreferencesInstance(
        fb2k::hwnd_t parent,
        preferences_page_callback::ptr callback
    ) : m_callback(std::move(callback)) {
        const auto loaded = wam_settings::load_ini_values();
        m_baseline = loaded.values;
        m_needsNormalization = loaded.needsNormalization;
        const auto instance = core_api::get_my_instance();
        register_window_class(instance);

        RECT client{};
        GetClientRect(parent, &client);
        m_window = CreateWindowExW(
            WS_EX_CONTROLPARENT,
            kWindowClass,
            L"",
            WS_CHILD | WS_VISIBLE | WS_CLIPCHILDREN,
            0,
            0,
            client.right - client.left,
            client.bottom - client.top,
            parent,
            nullptr,
            instance,
            this
        );
        if (m_window != nullptr) {
            create_controls(instance, parent);
            m_darkHooks.AddDialogWithControls(m_window);
            RedrawWindow(
                m_window,
                nullptr,
                nullptr,
                RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN
            );
            populate(m_baseline);
            layout();
        }
    }

    ~WamPreferencesInstance() {
        if (m_window != nullptr && IsWindow(m_window)) DestroyWindow(m_window);
    }

    t_uint32 get_state() override {
        t_uint32 state = preferences_state::resettable |
            preferences_state::dark_mode_supported;
        if (m_needsNormalization || !equal_values(read_controls(), m_baseline)) {
            state |= preferences_state::changed;
            state |= preferences_state::needs_restart_playback;
        }
        return state;
    }

    fb2k::hwnd_t get_wnd() override { return m_window; }

    void apply() override {
        const Values requested = read_controls();
        if (!write_values(requested)) {
            console::printf("WAM Bridge Output: could not save foobar.ini preferences");
            popup_message::g_show(
                "Could not save the WAM Bridge preferences file.",
                "WAM Bridge",
                popup_message::icon_error
            );
            return;
        }
        const auto loaded = wam_settings::load_ini_values();
        m_baseline = loaded.values;
        m_needsNormalization = loaded.needsNormalization;
        populate(m_baseline);
        m_callback->on_state_changed();
    }

    void reset() override {
        populate(default_values());
        m_callback->on_state_changed();
    }

private:
    static void register_window_class(HINSTANCE instance) {
        static bool registered = false;
        if (registered) return;
        WNDCLASSEXW windowClass{};
        windowClass.cbSize = sizeof(windowClass);
        windowClass.lpfnWndProc = &window_proc;
        windowClass.hInstance = instance;
        windowClass.hCursor = LoadCursorW(nullptr, IDC_ARROW);
        windowClass.hbrBackground = reinterpret_cast<HBRUSH>(
            static_cast<INT_PTR>(COLOR_BTNFACE + 1)
        );
        windowClass.lpszClassName = kWindowClass;
        if (RegisterClassExW(&windowClass) != 0 ||
            GetLastError() == ERROR_CLASS_ALREADY_EXISTS) {
            registered = true;
        }
    }

    static LRESULT CALLBACK window_proc(
        HWND window,
        UINT message,
        WPARAM wParam,
        LPARAM lParam
    ) {
        auto* self = reinterpret_cast<WamPreferencesInstance*>(
            GetWindowLongPtrW(window, GWLP_USERDATA)
        );
        if (message == WM_NCCREATE) {
            const auto* create = reinterpret_cast<const CREATESTRUCTW*>(lParam);
            self = static_cast<WamPreferencesInstance*>(create->lpCreateParams);
            SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(self));
        }
        if (self != nullptr) {
            if (message == WM_COMMAND) self->on_command(wParam, lParam);
            if (message == WM_SIZE) self->layout();
            if (message == WM_ERASEBKGND) {
                return self->erase_background(reinterpret_cast<HDC>(wParam));
            }
            if (message == WM_DESTROY) self->m_window = nullptr;
        }
        return DefWindowProcW(window, message, wParam, lParam);
    }

    HWND make_label(HINSTANCE instance, const wchar_t* text) {
        HWND control = CreateWindowExW(
            0,
            L"STATIC",
            text,
            WS_CHILD | WS_VISIBLE | SS_LEFT,
            0,
            0,
            0,
            0,
            m_window,
            nullptr,
            instance,
            nullptr
        );
        set_font(control);
        return control;
    }

    HWND make_edit(HINSTANCE instance, int id, bool numeric = false) {
        DWORD style = WS_CHILD | WS_VISIBLE | WS_TABSTOP | WS_BORDER | ES_AUTOHSCROLL;
        if (numeric) style |= ES_NUMBER;
        HWND control = CreateWindowExW(
            WS_EX_CLIENTEDGE,
            L"EDIT",
            L"",
            style,
            0,
            0,
            0,
            0,
            m_window,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),
            instance,
            nullptr
        );
        set_font(control);
        return control;
    }

    HWND make_checkbox(HINSTANCE instance, int id, const wchar_t* text) {
        HWND control = CreateWindowExW(
            0,
            L"BUTTON",
            text,
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_AUTOCHECKBOX,
            0,
            0,
            0,
            0,
            m_window,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(id)),
            instance,
            nullptr
        );
        set_font(control);
        return control;
    }

    void create_controls(HINSTANCE instance, HWND parent) {
        m_font = reinterpret_cast<HFONT>(SendMessageW(parent, WM_GETFONT, 0, 0));
        if (m_font == nullptr) {
            m_font = reinterpret_cast<HFONT>(GetStockObject(DEFAULT_GUI_FONT));
        }

        m_title = make_label(instance, L"WAM Bridge");
        m_note = make_label(
            instance,
            has_environment_overrides()
                ? L"WAMBRIDGE_* overrides are active and take precedence. Changes apply on next playback."
                : L"Environment variables override values saved here. Changes apply on next playback."
        );
        m_path = make_label(instance, (L"Config: " + config_path()).c_str());

        m_deviceLabel = make_label(instance, L"Device profile");
        m_device = make_edit(instance, kDevice);

        m_formatLabel = make_label(instance, L"Stream format");
        m_format = CreateWindowExW(
            0,
            L"COMBOBOX",
            L"",
            WS_CHILD | WS_VISIBLE | WS_TABSTOP | CBS_DROPDOWNLIST | WS_VSCROLL,
            0,
            0,
            0,
            0,
            m_window,
            reinterpret_cast<HMENU>(static_cast<INT_PTR>(kFormat)),
            instance,
            nullptr
        );
        set_font(m_format);
        for (const auto* format : {L"flac", L"wav", L"wav24", L"mp3"}) {
            SendMessageW(m_format, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(format));
        }

        m_volumeLabel = make_label(instance, L"Startup volume (raw 0-30, blank = unchanged)");
        m_volume = make_edit(instance, kVolume, true);
        m_hardwareVolume = make_checkbox(
            instance,
            kHardwareVolume,
            L"Route foobar volume slider to speaker"
        );

        m_volumeMaxLabel = make_label(instance, L"Volume ceiling (raw 1-30)");
        m_volumeMax = make_edit(instance, kVolumeMax, true);
        m_startVolumeMaxLabel = make_label(instance, L"Safe start cap (raw 0-30, 0 = off)");
        m_startVolumeMax = make_edit(instance, kStartVolumeMax, true);
        m_startupSilenceLabel = make_label(instance, L"Startup silence (ms, 0-10000)");
        m_startupSilence = make_edit(instance, kStartupSilence, true);
        m_bufferExtraLabel = make_label(instance, L"Extra buffer (ms, 0-10000)");
        m_bufferExtra = make_edit(instance, kBufferExtra, true);
        m_sleepAfterStopLabel = make_label(instance, L"Sleep after stop (s, 0-86400)");
        m_sleepAfterStop = make_edit(instance, kSleepAfterStop, true);
        m_diagnostics = make_checkbox(instance, kDiagnostics, L"Enable CLOCK diagnostics");

        m_helperLabel = make_label(instance, L"PCM helper override (blank = bundled)");
        m_helper = make_edit(instance, kHelper);
    }

    void set_font(HWND control) const {
        if (control != nullptr && m_font != nullptr) {
            SendMessageW(control, WM_SETFONT, reinterpret_cast<WPARAM>(m_font), TRUE);
        }
    }

    LRESULT erase_background(HDC dc) const {
        if (dc == nullptr || m_window == nullptr) return 0;
        RECT client{};
        GetClientRect(m_window, &client);
        HBRUSH brush = reinterpret_cast<HBRUSH>(SendMessageW(
            m_window,
            WM_CTLCOLORDLG,
            reinterpret_cast<WPARAM>(dc),
            reinterpret_cast<LPARAM>(m_window)
        ));
        if (brush == nullptr) brush = GetSysColorBrush(COLOR_BTNFACE);
        FillRect(dc, &client, brush);
        return 1;
    }

    int dpi_scale(int value) const {
        UINT dpi = 96;
        HDC dc = GetDC(m_window);
        if (dc != nullptr) {
            const int measured = GetDeviceCaps(dc, LOGPIXELSX);
            if (measured > 0) dpi = static_cast<UINT>(measured);
            ReleaseDC(m_window, dc);
        }
        return MulDiv(value, static_cast<int>(dpi), 96);
    }

    void place_row(HWND label, HWND control, int row, int height = 23) const {
        RECT client{};
        GetClientRect(m_window, &client);
        const int margin = dpi_scale(14);
        const int labelWidth = dpi_scale(250);
        const int gap = dpi_scale(10);
        const int top = dpi_scale(kRowBase + row * kRowPitch);
        const int controlX = margin + labelWidth + gap;
        const int availableWidth = static_cast<int>(client.right) - controlX - margin;
        const int controlWidth = (std::max)(dpi_scale(120), availableWidth);
        MoveWindow(label, margin, top + dpi_scale(4), labelWidth, dpi_scale(20), TRUE);
        MoveWindow(control, controlX, top, controlWidth, dpi_scale(height), TRUE);
    }

    void layout() const {
        if (m_window == nullptr) return;
        RECT client{};
        GetClientRect(m_window, &client);
        const int margin = dpi_scale(14);
        const int availableWidth = static_cast<int>(client.right) - margin * 2;
        const int width = (std::max)(dpi_scale(100), availableWidth);
        MoveWindow(m_title, margin, dpi_scale(8), width, dpi_scale(20), TRUE);
        MoveWindow(m_note, margin, dpi_scale(29), width, dpi_scale(28), TRUE);
        MoveWindow(m_path, margin, dpi_scale(58), width, dpi_scale(18), TRUE);

        place_row(m_deviceLabel, m_device, 0);
        place_row(m_formatLabel, m_format, 1, 200);
        place_row(m_volumeLabel, m_volume, 2);

        const int checkboxTop = dpi_scale(kRowBase + 3 * kRowPitch);
        MoveWindow(
            m_hardwareVolume,
            margin,
            checkboxTop,
            width,
            dpi_scale(23),
            TRUE
        );

        place_row(m_volumeMaxLabel, m_volumeMax, 4);
        place_row(m_startVolumeMaxLabel, m_startVolumeMax, 5);
        place_row(m_startupSilenceLabel, m_startupSilence, 6);
        place_row(m_bufferExtraLabel, m_bufferExtra, 7);
        place_row(m_sleepAfterStopLabel, m_sleepAfterStop, 8);

        const int diagnosticsTop = dpi_scale(kRowBase + 9 * kRowPitch);
        MoveWindow(m_diagnostics, margin, diagnosticsTop, width, dpi_scale(23), TRUE);
        place_row(m_helperLabel, m_helper, 10);
    }

    static void set_text(HWND control, const std::wstring& value) {
        SetWindowTextW(control, value.c_str());
    }

    static void set_checked(HWND control, bool checkedValue) {
        SendMessageW(
            control,
            BM_SETCHECK,
            checkedValue ? BST_CHECKED : BST_UNCHECKED,
            0
        );
    }

    static bool is_checked(HWND control) {
        return SendMessageW(control, BM_GETCHECK, 0, 0) == BST_CHECKED;
    }

    void populate(const Values& values) {
        m_loading = true;
        set_text(m_device, values.device);
        const int formatIndex = values.format == L"wav" ? 1 :
            values.format == L"wav24" ? 2 : values.format == L"mp3" ? 3 : 0;
        SendMessageW(m_format, CB_SETCURSEL, formatIndex, 0);
        set_text(
            m_volume,
            values.volume.has_value() ? std::to_wstring(*values.volume) : L""
        );
        set_checked(m_hardwareVolume, values.hardwareVolume);
        set_text(m_volumeMax, std::to_wstring(values.volumeMax));
        set_text(m_startVolumeMax, std::to_wstring(values.startVolumeMax));
        set_text(m_startupSilence, std::to_wstring(values.startupSilenceMs));
        set_text(m_bufferExtra, std::to_wstring(values.bufferExtraMs));
        set_text(m_sleepAfterStop, std::to_wstring(values.sleepAfterStopSeconds));
        set_checked(m_diagnostics, values.diagnostics);
        set_text(m_helper, values.helper);
        m_loading = false;
    }

    int control_int(HWND control, int fallback, int minimum, int maximum) const {
        return clamped_int(window_text(control), fallback, minimum, maximum);
    }

    Values read_controls() const {
        Values values;
        values.device = window_text(m_device);
        if (values.device.empty()) values.device = L"M5";

        const LRESULT selection = SendMessageW(m_format, CB_GETCURSEL, 0, 0);
        values.format = selection == 1 ? L"wav" :
            selection == 2 ? L"wav24" : selection == 3 ? L"mp3" : L"flac";

        const auto volume = window_text(m_volume);
        if (!volume.empty()) {
            values.volume = clamped_int(
                volume,
                0,
                0,
                kMaximumRawVolume
            );
        }
        values.hardwareVolume = is_checked(m_hardwareVolume);
        values.volumeMax = control_int(m_volumeMax, kDefaultVolumeMax, 1, 30);
        values.startVolumeMax = control_int(
            m_startVolumeMax,
            kDefaultStartVolumeMax,
            0,
            kMaximumRawVolume
        );
        values.startupSilenceMs = control_int(
            m_startupSilence,
            kDefaultStartupSilenceMs,
            0,
            kMaximumStartupSilenceMs
        );
        values.bufferExtraMs = control_int(
            m_bufferExtra,
            kDefaultBufferExtraMs,
            0,
            kMaximumBufferExtraMs
        );
        values.sleepAfterStopSeconds = control_int(
            m_sleepAfterStop,
            kDefaultSleepAfterStopSeconds,
            0,
            kMaximumSleepAfterStopSeconds
        );
        values.diagnostics = is_checked(m_diagnostics);
        values.helper = window_text(m_helper);
        return values;
    }

    void on_command(WPARAM wParam, LPARAM) {
        if (m_loading) return;
        const int code = HIWORD(wParam);
        if (code == EN_CHANGE || code == CBN_SELCHANGE || code == BN_CLICKED) {
            m_callback->on_state_changed();
        }
    }

    preferences_page_callback::ptr m_callback;
    Values m_baseline;
    bool m_needsNormalization = false;
    bool m_loading = false;
    fb2k::CCoreDarkModeHooks m_darkHooks;
    HWND m_window = nullptr;
    HFONT m_font = nullptr;

    HWND m_title = nullptr;
    HWND m_note = nullptr;
    HWND m_path = nullptr;
    HWND m_deviceLabel = nullptr;
    HWND m_device = nullptr;
    HWND m_formatLabel = nullptr;
    HWND m_format = nullptr;
    HWND m_volumeLabel = nullptr;
    HWND m_volume = nullptr;
    HWND m_hardwareVolume = nullptr;
    HWND m_volumeMaxLabel = nullptr;
    HWND m_volumeMax = nullptr;
    HWND m_startVolumeMaxLabel = nullptr;
    HWND m_startVolumeMax = nullptr;
    HWND m_startupSilenceLabel = nullptr;
    HWND m_startupSilence = nullptr;
    HWND m_bufferExtraLabel = nullptr;
    HWND m_bufferExtra = nullptr;
    HWND m_sleepAfterStopLabel = nullptr;
    HWND m_sleepAfterStop = nullptr;
    HWND m_diagnostics = nullptr;
    HWND m_helperLabel = nullptr;
    HWND m_helper = nullptr;
};

class WamPreferencesPage : public preferences_page_v4 {
public:
    const char* get_name() override { return "WAM Bridge"; }
    GUID get_guid() override { return kPreferencesGuid; }
    GUID get_parent_guid() override { return preferences_page::guid_output; }

    preferences_page_instance::ptr instantiate(
        fb2k::hwnd_t parent,
        preferences_page_callback::ptr callback
    ) override {
        return new service_impl_t<WamPreferencesInstance>(parent, callback);
    }
};

preferences_page_factory_t<WamPreferencesPage> g_preferencesFactory;

}  // namespace
