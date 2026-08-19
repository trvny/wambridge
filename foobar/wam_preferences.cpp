#include <windows.h>

#include <foobar2000/SDK/foobar2000.h>

#include <algorithm>
#include <array>
#include <cwchar>
#include <string>

namespace {

constexpr wchar_t kSection[] = L"wambridge";
constexpr wchar_t kWindowClass[] = L"WAMBridgePreferencesPage";
constexpr int kDefaultStartupSilenceMs = 1500;
constexpr int kDefaultBufferExtraMs = 2000;
constexpr int kDefaultVolumeMax = 10;
constexpr int kDefaultStartVolumeMax = 3;
constexpr int kDefaultSleepAfterStopSeconds = 0;

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

struct Values {
    std::wstring device = L"M5";
    std::wstring format = L"flac";
    std::wstring volume;
    bool hardwareVolume = false;
    int volumeMax = kDefaultVolumeMax;
    int startVolumeMax = kDefaultStartVolumeMax;
    int startupSilenceMs = kDefaultStartupSilenceMs;
    int bufferExtraMs = kDefaultBufferExtraMs;
    int sleepAfterStopSeconds = kDefaultSleepAfterStopSeconds;
    bool diagnostics = false;
    std::wstring helper;
};

bool equal_values(const Values& left, const Values& right) {
    return left.device == right.device &&
        left.format == right.format &&
        left.volume == right.volume &&
        left.hardwareVolume == right.hardwareVolume &&
        left.volumeMax == right.volumeMax &&
        left.startVolumeMax == right.startVolumeMax &&
        left.startupSilenceMs == right.startupSilenceMs &&
        left.bufferExtraMs == right.bufferExtraMs &&
        left.sleepAfterStopSeconds == right.sleepAfterStopSeconds &&
        left.diagnostics == right.diagnostics &&
        left.helper == right.helper;
}

std::wstring environment_value(const wchar_t* name) {
    const DWORD needed = GetEnvironmentVariableW(name, nullptr, 0);
    if (needed == 0) return {};
    std::wstring value(needed, L'\0');
    const DWORD written = GetEnvironmentVariableW(name, value.data(), needed);
    if (written == 0 || written >= needed) return {};
    value.resize(written);
    return value;
}

std::wstring config_path() {
    const auto localAppData = environment_value(L"LOCALAPPDATA");
    if (localAppData.empty()) return L"foobar.ini";
    return localAppData + L"\\WAMBridge\\foobar.ini";
}

std::wstring ini_value(const wchar_t* key, const wchar_t* fallback = L"") {
    std::array<wchar_t, 32768> buffer{};
    const auto path = config_path();
    const DWORD size = GetPrivateProfileStringW(
        kSection,
        key,
        fallback,
        buffer.data(),
        static_cast<DWORD>(buffer.size()),
        path.c_str()
    );
    return std::wstring(buffer.data(), size);
}

bool truthy(const std::wstring& value) {
    return value == L"1" || value == L"true" || value == L"yes" ||
        value == L"on";
}

int parsed_int(const std::wstring& value, int fallback, int minimum, int maximum) {
    if (value.empty()) return fallback;
    wchar_t* end = nullptr;
    const long parsed = std::wcstol(value.c_str(), &end, 10);
    if (end == value.c_str() || *end != L'\0') return fallback;
    return std::clamp(static_cast<int>(parsed), minimum, maximum);
}

Values default_values() {
    return {};
}

Values load_values() {
    Values values;
    values.device = ini_value(L"device", L"M5");
    if (values.device.empty()) values.device = L"M5";

    values.format = ini_value(L"format", L"");
    const bool knownFormat = values.format == L"flac" || values.format == L"wav" ||
        values.format == L"wav24" || values.format == L"mp3";
    if (!knownFormat) values.format = L"flac";

    const auto volume = ini_value(L"volume", L"");
    if (!volume.empty()) {
        wchar_t* end = nullptr;
        const long parsed = std::wcstol(volume.c_str(), &end, 10);
        if (end != volume.c_str() && *end == L'\0' && parsed >= 0 && parsed <= 100) {
            values.volume = std::to_wstring(parsed);
        }
    }

    values.hardwareVolume = truthy(ini_value(L"hardware_volume", L""));
    values.volumeMax = parsed_int(
        ini_value(L"volume_max", L""),
        kDefaultVolumeMax,
        1,
        30
    );
    values.startVolumeMax = parsed_int(
        ini_value(L"start_volume_max", L""),
        kDefaultStartVolumeMax,
        0,
        30
    );
    values.startupSilenceMs = parsed_int(
        ini_value(L"startup_silence", L""),
        kDefaultStartupSilenceMs,
        0,
        10000
    );
    values.bufferExtraMs = parsed_int(
        ini_value(L"buffer_extra", L""),
        kDefaultBufferExtraMs,
        0,
        10000
    );
    values.sleepAfterStopSeconds = parsed_int(
        ini_value(L"sleep_after_stop", L""),
        kDefaultSleepAfterStopSeconds,
        0,
        86400
    );
    values.diagnostics = truthy(ini_value(L"diagnostics", L""));
    values.helper = ini_value(L"helper", L"");
    return values;
}

bool ensure_config_directory() {
    const auto path = config_path();
    const auto separator = path.find_last_of(L"\\/");
    if (separator == std::wstring::npos) return true;
    const auto directory = path.substr(0, separator);
    if (CreateDirectoryW(directory.c_str(), nullptr)) return true;
    return GetLastError() == ERROR_ALREADY_EXISTS;
}

bool write_setting(
    const std::wstring& path,
    const wchar_t* key,
    const std::wstring& value,
    const wchar_t* defaultValue
) {
    const wchar_t* stored = value == defaultValue ? nullptr : value.c_str();
    return WritePrivateProfileStringW(kSection, key, stored, path.c_str()) != FALSE;
}

bool write_values(const Values& values) {
    if (!ensure_config_directory()) return false;
    const auto path = config_path();
    bool ok = true;
    ok = write_setting(path, L"device", values.device, L"M5") && ok;
    ok = write_setting(path, L"format", values.format, L"flac") && ok;
    ok = write_setting(path, L"volume", values.volume, L"") && ok;
    ok = write_setting(
        path,
        L"hardware_volume",
        values.hardwareVolume ? L"1" : L"",
        L""
    ) && ok;
    ok = write_setting(
        path,
        L"volume_max",
        std::to_wstring(values.volumeMax),
        L"10"
    ) && ok;
    ok = write_setting(
        path,
        L"start_volume_max",
        std::to_wstring(values.startVolumeMax),
        L"3"
    ) && ok;
    ok = write_setting(
        path,
        L"startup_silence",
        std::to_wstring(values.startupSilenceMs),
        L"1500"
    ) && ok;
    ok = write_setting(
        path,
        L"buffer_extra",
        std::to_wstring(values.bufferExtraMs),
        L"2000"
    ) && ok;
    ok = write_setting(
        path,
        L"sleep_after_stop",
        std::to_wstring(values.sleepAfterStopSeconds),
        L"0"
    ) && ok;
    ok = write_setting(
        path,
        L"diagnostics",
        values.diagnostics ? L"1" : L"",
        L""
    ) && ok;
    ok = write_setting(path, L"helper", values.helper, L"") && ok;
    WritePrivateProfileStringW(nullptr, nullptr, nullptr, path.c_str());
    return ok;
}

bool has_environment_overrides() {
    constexpr const wchar_t* names[] = {
        L"WAMBRIDGE_PCM",
        L"WAMBRIDGE_CONTROL",
        L"WAMBRIDGE_DEVICE",
        L"WAMBRIDGE_VOLUME",
        L"WAMBRIDGE_FORMAT",
        L"WAMBRIDGE_DIAGNOSTICS",
        L"WAMBRIDGE_HARDWARE_VOLUME",
        L"WAMBRIDGE_VOLUME_MAX",
        L"WAMBRIDGE_START_VOLUME_MAX",
        L"WAMBRIDGE_STARTUP_SILENCE",
        L"WAMBRIDGE_BUFFER_EXTRA",
        L"WAMBRIDGE_SLEEP_AFTER_STOP",
    };
    for (const auto* name : names) {
        if (!environment_value(name).empty()) return true;
    }
    return false;
}

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
    ) : m_callback(std::move(callback)), m_baseline(load_values()) {
        const auto instance = core_api::get_my_instance();
        register_window_class(instance);

        RECT client{};
        GetClientRect(parent, &client);
        m_window = CreateWindowExW(
            0,
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
            populate(m_baseline);
            layout();
        }
    }

    ~WamPreferencesInstance() override {
        if (m_window != nullptr && IsWindow(m_window)) DestroyWindow(m_window);
    }

    t_uint32 get_state() override {
        t_uint32 state = preferences_state::resettable;
        if (!equal_values(read_controls(), m_baseline)) {
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
            return;
        }
        m_baseline = load_values();
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
        windowClass.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_BTNFACE + 1);
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
        if (m_font == nullptr) m_font = static_cast<HFONT>(GetStockObject(DEFAULT_GUI_FONT));

        m_title = make_label(instance, L"WAM Bridge");
        m_note = make_label(
            instance,
            has_environment_overrides()
                ? L"WAMBRIDGE_* environment overrides are active and take precedence. Changes apply to the next playback session."
                : L"Environment variables take precedence over values saved here. Changes apply to the next playback session."
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

        m_volumeLabel = make_label(instance, L"Startup volume (0-100, blank = unchanged)");
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
        const int top = dpi_scale(86 + row * 31);
        const int controlX = margin + labelWidth + gap;
        const int controlWidth = (std::max)(dpi_scale(120), client.right - controlX - margin);
        MoveWindow(label, margin, top + dpi_scale(4), labelWidth, dpi_scale(20), TRUE);
        MoveWindow(control, controlX, top, controlWidth, dpi_scale(height), TRUE);
    }

    void layout() const {
        if (m_window == nullptr) return;
        RECT client{};
        GetClientRect(m_window, &client);
        const int margin = dpi_scale(14);
        const int width = (std::max)(dpi_scale(100), client.right - margin * 2);
        MoveWindow(m_title, margin, dpi_scale(12), width, dpi_scale(22), TRUE);
        MoveWindow(m_note, margin, dpi_scale(35), width, dpi_scale(20), TRUE);
        MoveWindow(m_path, margin, dpi_scale(57), width, dpi_scale(20), TRUE);

        place_row(m_deviceLabel, m_device, 0);
        place_row(m_formatLabel, m_format, 1, 200);
        place_row(m_volumeLabel, m_volume, 2);

        const int checkboxTop = dpi_scale(86 + 3 * 31);
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

        const int diagnosticsTop = dpi_scale(86 + 9 * 31);
        MoveWindow(m_diagnostics, margin, diagnosticsTop, width, dpi_scale(23), TRUE);
        place_row(m_helperLabel, m_helper, 10);
    }

    static void set_text(HWND control, const std::wstring& value) {
        SetWindowTextW(control, value.c_str());
    }

    static void set_checked(HWND control, bool checked) {
        SendMessageW(control, BM_SETCHECK, checked ? BST_CHECKED : BST_UNCHECKED, 0);
    }

    static bool checked(HWND control) {
        return SendMessageW(control, BM_GETCHECK, 0, 0) == BST_CHECKED;
    }

    void populate(const Values& values) {
        m_loading = true;
        set_text(m_device, values.device);
        const int formatIndex = values.format == L"wav" ? 1 :
            values.format == L"wav24" ? 2 : values.format == L"mp3" ? 3 : 0;
        SendMessageW(m_format, CB_SETCURSEL, formatIndex, 0);
        set_text(m_volume, values.volume);
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
        return parsed_int(window_text(control), fallback, minimum, maximum);
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
            values.volume = std::to_wstring(parsed_int(volume, 0, 0, 100));
        }
        values.hardwareVolume = checked(m_hardwareVolume);
        values.volumeMax = control_int(m_volumeMax, kDefaultVolumeMax, 1, 30);
        values.startVolumeMax = control_int(
            m_startVolumeMax,
            kDefaultStartVolumeMax,
            0,
            30
        );
        values.startupSilenceMs = control_int(
            m_startupSilence,
            kDefaultStartupSilenceMs,
            0,
            10000
        );
        values.bufferExtraMs = control_int(
            m_bufferExtra,
            kDefaultBufferExtraMs,
            0,
            10000
        );
        values.sleepAfterStopSeconds = control_int(
            m_sleepAfterStop,
            kDefaultSleepAfterStopSeconds,
            0,
            86400
        );
        values.diagnostics = checked(m_diagnostics);
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
    bool m_loading = false;
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
