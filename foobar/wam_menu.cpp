#include <winsock2.h>
#include <windows.h>
#include <mmsystem.h>
#include <objidl.h>

#include <foobar2000/SDK/foobar2000.h>

#include "wam_control.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cwchar>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr char kComponentName[] = "WAM Bridge Output";
constexpr int kDefaultSafeVolume = 3;
constexpr int kMaximumRawVolume = 30;
constexpr int kMaximumLegacyVolume = 100;
constexpr size_t kMaximumLoggedOutput = 2000;

// Used only as a stable address inside this component module.
// {FC099CF9-0DA5-4F52-A5DD-F8DF35EA6C55}
constexpr GUID kMenuModuleGuid = {
    0xfc099cf9,
    0x0da5,
    0x4f52,
    {0xa5, 0xdd, 0xf8, 0xdf, 0x35, 0xea, 0x6c, 0x55},
};

// {72F78E03-3B8B-41F5-B61B-7EE7A47D4C86}
constexpr GUID kMenuGroupGuid = {
    0x72f78e03,
    0x3b8b,
    0x41f5,
    {0xb6, 0x1b, 0x7e, 0xe7, 0xa4, 0x7d, 0x4c, 0x86},
};

// {28F99F89-5A55-474E-9F37-727E62434414}
constexpr GUID kEmergencyStopGuid = {
    0x28f99f89,
    0x5a55,
    0x474e,
    {0x9f, 0x37, 0x72, 0x7e, 0x62, 0x43, 0x44, 0x14},
};

// {E73DAFF3-4651-4C89-815B-3D17D793E012}
constexpr GUID kStandbyGuid = {
    0xe73daff3,
    0x4651,
    0x4c89,
    {0x81, 0x5b, 0x3d, 0x17, 0xd7, 0x93, 0xe0, 0x12},
};

// {A78ED2FB-588B-4112-9433-057F2F52F66C}
constexpr GUID kVolumeUpGuid = {
    0xa78ed2fb,
    0x588b,
    0x4112,
    {0x94, 0x33, 0x05, 0x7f, 0x2f, 0x52, 0xf6, 0x6c},
};

// {6414A53C-BEA1-47CC-AE39-D8700C35A34B}
constexpr GUID kVolumeDownGuid = {
    0x6414a53c,
    0xbea1,
    0x47cc,
    {0xae, 0x39, 0xd8, 0x70, 0x0c, 0x35, 0xa3, 0x4b},
};

// {EEBBAA34-04C3-47EE-8908-B7F952E302F0}
constexpr GUID kSafeVolumeGuid = {
    0xeebbaa34,
    0x04c3,
    0x47ee,
    {0x89, 0x08, 0xb7, 0xf9, 0x52, 0xe3, 0x02, 0xf0},
};

struct MenuItem {
    GUID guid;
    const char* name;
    const char* description;
    const wchar_t* action;
    bool stopsFoobar;
};

constexpr std::array<MenuItem, 5> kMenuItems = {{
    {
        kEmergencyStopGuid,
        "Emergency stop",
        "Stop foobar and the speaker, unmute it and restore the configured safe volume.",
        L"emergency-stop",
        true,
    },
    {
        kStandbyGuid,
        "Stop & mute",
        "Stop foobar and mute the Samsung WAM speaker without powering it down.",
        L"standby",
        true,
    },
    {
        kVolumeUpGuid,
        "Volume up",
        "Raise the physical Samsung WAM volume by one raw step.",
        L"volume-up",
        false,
    },
    {
        kVolumeDownGuid,
        "Volume down",
        "Lower the physical Samsung WAM volume by one raw step.",
        L"volume-down",
        false,
    },
    {
        kSafeVolumeGuid,
        "Volume to safe level",
        "Set the physical Samsung WAM volume to the configured startup level.",
        L"safe-volume",
        false,
    },
}};

void close_handle(HANDLE& handle) {
    if (handle != nullptr && handle != INVALID_HANDLE_VALUE) {
        CloseHandle(handle);
        handle = nullptr;
    }
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

bool file_exists(const std::wstring& path) {
    const DWORD attributes = GetFileAttributesW(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES &&
        (attributes & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

std::wstring module_directory() {
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&kMenuModuleGuid),
            &module
        )) {
        return {};
    }

    std::vector<wchar_t> buffer(32768);
    const DWORD size = GetModuleFileNameW(
        module,
        buffer.data(),
        static_cast<DWORD>(buffer.size())
    );
    if (size == 0 || size >= buffer.size()) return {};

    std::wstring path(buffer.data(), size);
    const size_t separator = path.find_last_of(L"\\/");
    if (separator == std::wstring::npos) return {};
    return path.substr(0, separator);
}

std::wstring config_path() {
    const auto localAppData = environment_value(L"LOCALAPPDATA");
    if (localAppData.empty()) return L"foobar.ini";
    return localAppData + L"\\WAMBridge\\foobar.ini";
}

std::wstring ini_value(
    const wchar_t* key,
    const wchar_t* fallback,
    const std::wstring& path
) {
    std::vector<wchar_t> buffer(32768);
    const DWORD size = GetPrivateProfileStringW(
        L"wambridge",
        key,
        fallback,
        buffer.data(),
        static_cast<DWORD>(buffer.size()),
        path.c_str()
    );
    return std::wstring(buffer.data(), size);
}

std::wstring quoted(const std::wstring& value) {
    std::wstring result = L"\"";
    size_t slashes = 0;
    for (const wchar_t character : value) {
        if (character == L'\\') {
            ++slashes;
            continue;
        }
        if (character == L'\"') {
            result.append(slashes * 2 + 1, L'\\');
            result.push_back(L'\"');
            slashes = 0;
            continue;
        }
        result.append(slashes, L'\\');
        slashes = 0;
        result.push_back(character);
    }
    result.append(slashes * 2, L'\\');
    result.push_back(L'\"');
    return result;
}

// A bare file name would leave CreateProcessW resolving the helper against the
// working directory, so only the configured override or the absolute bundled
// path is ever used. An empty result fails the launch with a console error.
std::wstring control_helper_path() {
    const auto overridePath = environment_value(L"WAMBRIDGE_CONTROL");
    if (!overridePath.empty()) return overridePath;

    const auto directory = module_directory();
    if (directory.empty()) return {};
    const auto bundled = directory +
        L"\\wambridge-control\\wambridge-control.exe";
    return file_exists(bundled) ? bundled : std::wstring{};
}

std::wstring configured_device() {
    auto device = environment_value(L"WAMBRIDGE_DEVICE");
    if (!device.empty()) return device;
    return ini_value(L"device", L"M5", config_path());
}

int configured_safe_volume() {
    auto raw = environment_value(L"WAMBRIDGE_VOLUME");
    if (raw.empty()) raw = ini_value(L"volume", L"", config_path());
    if (raw.empty()) return kDefaultSafeVolume;

    wchar_t* end = nullptr;
    const long parsed = std::wcstol(raw.c_str(), &end, 10);
    if (end == raw.c_str() || *end != L'\0' || parsed < 0 ||
        parsed > kMaximumLegacyVolume) {
        return kDefaultSafeVolume;
    }
    return static_cast<int>(std::min<long>(parsed, kMaximumRawVolume));
}

std::string compact_output(std::string output) {
    for (char& character : output) {
        if (character == '\r' || character == '\n' || character == '\t') {
            character = ' ';
        }
    }
    const auto first = output.find_first_not_of(' ');
    if (first == std::string::npos) return {};
    const auto last = output.find_last_not_of(' ');
    output = output.substr(first, last - first + 1);
    if (output.size() > kMaximumLoggedOutput) {
        output.resize(kMaximumLoggedOutput);
        output += "...";
    }
    return output;
}

// The slider is dragged, not clicked. Sending every intermediate level would
// spawn a control helper per pixel and flood the shared 55001 port, so the
// dispatcher keeps only the newest pending level and spaces sends out.
constexpr auto kVolumeSendInterval = std::chrono::milliseconds(250);

struct ControlAction {
    std::wstring name;
    std::optional<int> level;
};

std::string action_label(const std::wstring& action) {
    std::string label;
    label.reserve(action.size());
    for (const wchar_t character : action) {
        label.push_back(
            character >= 0 && character <= 0x7f
                ? static_cast<char>(character)
                : '?'
        );
    }
    return label;
}

class ControlDispatcher {
public:
    ControlDispatcher() : m_worker(&ControlDispatcher::worker_loop, this) {}

    ~ControlDispatcher() {
        shutdown();
    }

    void enqueue(const wchar_t* action) {
        {
            std::lock_guard lock(m_mutex);
            if (m_shutdown) return;
            m_queue.push_back({action});
        }
        m_cv.notify_one();
        const auto label = action_label(action);
        console::printf("%s: queued %s", kComponentName, label.c_str());
    }

    // Replaces any level that has not been sent yet. Deliberately silent: one
    // console line per slider position would be thousands during a drag.
    void request_volume(int step) {
        {
            std::lock_guard lock(m_mutex);
            if (m_shutdown) return;
            m_pendingVolume = step;
        }
        m_cv.notify_one();
    }

    void shutdown() {
        HANDLE process = nullptr;
        {
            std::lock_guard lock(m_mutex);
            if (m_shutdown) return;
            m_shutdown = true;
            m_queue.clear();
            process = m_process;
        }
        if (process != nullptr) {
            TerminateProcess(process, ERROR_CANCELLED);
        }
        m_cv.notify_all();
        if (m_worker.joinable()) m_worker.join();
    }

private:
    std::wstring command_line(const ControlAction& action) const {
        const auto helper = control_helper_path();
        std::wstring command = quoted(helper);
        command += L" ";
        command += action.name;
        command += L" --device ";
        command += quoted(configured_device());
        command += L" --safe-volume ";
        command += std::to_wstring(configured_safe_volume());
        if (action.level.has_value()) {
            command += L" --level ";
            command += std::to_wstring(*action.level);
        }
        return command;
    }

    void report_result(
        const ControlAction& action,
        DWORD exitCode,
        const std::string& output
    ) const {
        const auto compact = compact_output(output);
        const auto label = action_label(action.name);
        if (exitCode == 0) {
            if (compact.empty()) {
                console::printf(
                    "%s: %s completed",
                    kComponentName,
                    label.c_str()
                );
            } else {
                console::printf(
                    "%s: %s completed: %s",
                    kComponentName,
                    label.c_str(),
                    compact.c_str()
                );
            }
            return;
        }

        // Only %u and %s: console::printf is pfc's formatter and prints the
        // length modifiers in %lu and %llu literally, dropping the value.
        if (compact.empty()) {
            console::printf(
                "%s: %s failed with exit code %u",
                kComponentName,
                label.c_str(),
                static_cast<unsigned>(exitCode)
            );
        } else {
            console::printf(
                "%s: %s failed with exit code %u: %s",
                kComponentName,
                label.c_str(),
                static_cast<unsigned>(exitCode),
                compact.c_str()
            );
        }
    }

    void run_action(const ControlAction& action) {
        // The helper already holds a connection to the speaker's control port.
        // Spawning a process to open a second one is what made this whole
        // approach unsafe during playback, so it is now the fallback for when
        // nothing is playing rather than the mechanism.
        if (action.level.has_value() &&
            wam::send_volume_over_helper(*action.level)) {
            return;
        }

        SECURITY_ATTRIBUTES security{};
        security.nLength = sizeof(security);
        security.bInheritHandle = TRUE;

        HANDLE outputRead = nullptr;
        HANDLE outputWrite = nullptr;
        HANDLE nullInput = CreateFileW(
            L"NUL",
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            &security,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            nullptr
        );
        if (nullInput == INVALID_HANDLE_VALUE ||
            !CreatePipe(&outputRead, &outputWrite, &security, 0)) {
            close_handle(nullInput);
            close_handle(outputRead);
            close_handle(outputWrite);
            console::printf(
                "%s: could not create control-helper pipes (Windows error %u)",
                kComponentName,
                static_cast<unsigned>(GetLastError())
            );
            return;
        }
        SetHandleInformation(outputRead, HANDLE_FLAG_INHERIT, 0);

        STARTUPINFOEXW startup{};
        startup.StartupInfo.cb = sizeof(startup);
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW;
        startup.StartupInfo.wShowWindow = SW_HIDE;
        startup.StartupInfo.hStdInput = nullInput;
        startup.StartupInfo.hStdOutput = outputWrite;
        startup.StartupInfo.hStdError = outputWrite;

        SIZE_T attributeListSize = 0;
        InitializeProcThreadAttributeList(nullptr, 1, 0, &attributeListSize);
        if (attributeListSize == 0) {
            close_handle(nullInput);
            close_handle(outputRead);
            close_handle(outputWrite);
            console::printf(
                "%s: could not prepare control-helper handle inheritance",
                kComponentName
            );
            return;
        }

        std::vector<std::byte> attributeStorage(attributeListSize);
        startup.lpAttributeList = reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(
            attributeStorage.data()
        );
        if (!InitializeProcThreadAttributeList(
                startup.lpAttributeList,
                1,
                0,
                &attributeListSize
            )) {
            close_handle(nullInput);
            close_handle(outputRead);
            close_handle(outputWrite);
            console::printf(
                "%s: could not initialize control-helper handle inheritance",
                kComponentName
            );
            return;
        }

        HANDLE inheritedHandles[] = {nullInput, outputWrite};
        if (!UpdateProcThreadAttribute(
                startup.lpAttributeList,
                0,
                PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                inheritedHandles,
                sizeof(inheritedHandles),
                nullptr,
                nullptr
            )) {
            DeleteProcThreadAttributeList(startup.lpAttributeList);
            close_handle(nullInput);
            close_handle(outputRead);
            close_handle(outputWrite);
            console::printf(
                "%s: could not restrict control-helper handles",
                kComponentName
            );
            return;
        }

        const auto helper = control_helper_path();
        auto command = command_line(action);
        std::vector<wchar_t> mutableCommand(command.begin(), command.end());
        mutableCommand.push_back(L'\0');

        PROCESS_INFORMATION process{};
        const BOOL started = CreateProcessW(
            helper.c_str(),
            mutableCommand.data(),
            nullptr,
            nullptr,
            TRUE,
            CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT |
                EXTENDED_STARTUPINFO_PRESENT,
            nullptr,
            nullptr,
            &startup.StartupInfo,
            &process
        );
        DeleteProcThreadAttributeList(startup.lpAttributeList);
        close_handle(nullInput);
        close_handle(outputWrite);
        if (!started) {
            close_handle(outputRead);
            console::printf(
                "%s: could not start wambridge-control.exe (Windows error %u)",
                kComponentName,
                static_cast<unsigned>(GetLastError())
            );
            return;
        }
        close_handle(process.hThread);

        {
            std::lock_guard lock(m_mutex);
            m_process = process.hProcess;
            if (m_shutdown) TerminateProcess(process.hProcess, ERROR_CANCELLED);
        }

        std::string output;
        std::array<char, 4096> buffer{};
        DWORD read = 0;
        while (ReadFile(
            outputRead,
            buffer.data(),
            static_cast<DWORD>(buffer.size()),
            &read,
            nullptr
        )) {
            if (read > 0) output.append(buffer.data(), read);
        }
        close_handle(outputRead);
        WaitForSingleObject(process.hProcess, INFINITE);

        DWORD exitCode = ERROR_GEN_FAILURE;
        GetExitCodeProcess(process.hProcess, &exitCode);
        {
            std::lock_guard lock(m_mutex);
            if (m_process == process.hProcess) m_process = nullptr;
        }
        close_handle(process.hProcess);

        bool shuttingDown = false;
        {
            std::lock_guard lock(m_mutex);
            shuttingDown = m_shutdown;
        }
        if (!shuttingDown) report_result(action, exitCode, output);
        if (exitCode == 0) {
            // Every volume action prints where the speaker ended up. Handing
            // that back is what keeps the slider from disagreeing with the
            // speaker after a menu press.
            const int step = reported_volume(output);
            if (step >= 0) wam::note_speaker_step(step);
        }
    }

    // "volume=<n>" out of the control helper's own output, or -1 when the
    // action did not report one.
    static int reported_volume(const std::string& output) {
        const std::string key = "volume=";
        const size_t at = output.rfind(key);
        if (at == std::string::npos) return -1;
        size_t index = at + key.size();
        int value = 0;
        bool any = false;
        while (index < output.size() && output[index] >= '0' && output[index] <= '9') {
            value = value * 10 + (output[index] - '0');
            index++;
            any = true;
        }
        if (!any || value > kMaximumRawVolume) return -1;
        return value;
    }

    // Menu actions win over slider levels: a queued emergency stop must not
    // wait behind a drag, and a stale level is worth dropping anyway.
    bool next_action_locked(std::unique_lock<std::mutex>& lock, ControlAction& out) {
        for (;;) {
            if (m_shutdown) return false;
            if (!m_queue.empty()) {
                out = std::move(m_queue.front());
                m_queue.pop_front();
                return true;
            }
            if (!m_pendingVolume.has_value()) {
                m_cv.wait(lock);
                continue;
            }
            const auto now = std::chrono::steady_clock::now();
            const auto ready = m_lastVolumeSent + kVolumeSendInterval;
            if (now < ready) {
                m_cv.wait_until(lock, ready);
                continue;
            }
            const int step = *m_pendingVolume;
            m_pendingVolume.reset();
            // The slider passes through levels that map to a step already set;
            // re-sending them would spend the control port on nothing.
            if (step == m_lastSentVolume) continue;
            m_lastSentVolume = step;
            m_lastVolumeSent = now;
            out = ControlAction{L"set-volume", step};
            return true;
        }
    }

    void worker_loop() {
        for (;;) {
            ControlAction action;
            {
                std::unique_lock lock(m_mutex);
                if (!next_action_locked(lock, action)) return;
            }
            run_action(action);
        }
    }

    std::mutex m_mutex;
    std::condition_variable m_cv;
    std::deque<ControlAction> m_queue;
    std::optional<int> m_pendingVolume;
    int m_lastSentVolume = -1;
    std::chrono::steady_clock::time_point m_lastVolumeSent{};
    std::thread m_worker;
    HANDLE m_process = nullptr;
    bool m_shutdown = false;
};

ControlDispatcher& control_dispatcher() {
    static ControlDispatcher dispatcher;
    return dispatcher;
}

class WamMenuCommands : public mainmenu_commands {
public:
    t_uint32 get_command_count() override {
        return static_cast<t_uint32>(kMenuItems.size());
    }

    GUID get_command(t_uint32 index) override {
        if (index >= kMenuItems.size()) return pfc::guid_null;
        return kMenuItems[index].guid;
    }

    void get_name(t_uint32 index, pfc::string_base& output) override {
        if (index < kMenuItems.size()) output = kMenuItems[index].name;
    }

    bool get_description(t_uint32 index, pfc::string_base& output) override {
        if (index >= kMenuItems.size()) return false;
        output = kMenuItems[index].description;
        return true;
    }

    GUID get_parent() override {
        return kMenuGroupGuid;
    }

    void execute(
        t_uint32 index,
        service_ptr_t<service_base>
    ) override {
        if (index >= kMenuItems.size()) return;
        const auto& item = kMenuItems[index];
        if (item.stopsFoobar) {
            static_api_ptr_t<playback_control> control;
            control->stop();
        }
        control_dispatcher().enqueue(item.action);
    }

    bool get_display(
        t_uint32 index,
        pfc::string_base& text,
        t_uint32& flags
    ) override {
        if (index >= kMenuItems.size()) return false;
        flags = 0;
        text = kMenuItems[index].name;
        return true;
    }
};

mainmenu_group_popup_factory g_wamMenuGroup(
    kMenuGroupGuid,
    mainmenu_groups::playback,
    mainmenu_commands::sort_priority_dontcare,
    "WAM Bridge"
);
mainmenu_commands_factory_t<WamMenuCommands> g_wamMenuCommands;

}  // namespace

namespace wam {

void request_volume_step(int step) {
    control_dispatcher().request_volume(
        std::max(0, std::min(kMaximumRawVolume, step))
    );
}

}  // namespace wam
