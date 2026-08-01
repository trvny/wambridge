#include <winsock2.h>
#include <windows.h>
#include <mmsystem.h>
#include <objidl.h>

#include <foobar2000/SDK/foobar2000.h>

#include <array>
#include <cstddef>
#include <cwchar>
#include <string>
#include <vector>

namespace {

constexpr char kComponentName[] = "WAM Bridge Output";
constexpr int kDefaultSafeVolume = 3;
constexpr int kMaximumRawVolume = 30;

// Used only as a stable address inside this component module.
// {FC099CF9-0DA5-4F52-A5DD-F8DF35EA6C55}
constexpr GUID kMenuModuleGuid = {
    0xfc099cf9,
    0x0da5,
    0x4f52,
    {0xa5, 0xdd, 0xf8, 0xdf, 0x35, 0xea, 0x6c, 0x55},
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
        "WAM Bridge: Emergency stop",
        "Stop foobar and the speaker, unmute it and restore the configured safe volume.",
        L"emergency-stop",
        true,
    },
    {
        kStandbyGuid,
        "WAM Bridge: Standby",
        "Stop foobar and leave the Samsung WAM speaker muted for standby.",
        L"standby",
        true,
    },
    {
        kVolumeUpGuid,
        "WAM Bridge: Volume up",
        "Raise the physical Samsung WAM volume by one raw step.",
        L"volume-up",
        false,
    },
    {
        kVolumeDownGuid,
        "WAM Bridge: Volume down",
        "Lower the physical Samsung WAM volume by one raw step.",
        L"volume-down",
        false,
    },
    {
        kSafeVolumeGuid,
        "WAM Bridge: Volume to safe level",
        "Set the physical Samsung WAM volume to the configured startup level.",
        L"safe-volume",
        false,
    },
}};

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

std::wstring control_helper_path() {
    const auto overridePath = environment_value(L"WAMBRIDGE_CONTROL");
    if (!overridePath.empty()) return overridePath;

    const auto directory = module_directory();
    if (!directory.empty()) {
        const auto bundled = directory +
            L"\\wambridge-control\\wambridge-control.exe";
        if (file_exists(bundled)) return bundled;
    }
    return L"wambridge-control.exe";
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
        parsed > kMaximumRawVolume) {
        return kDefaultSafeVolume;
    }
    return static_cast<int>(parsed);
}

bool launch_control(const wchar_t* action) {
    const auto helper = control_helper_path();
    std::wstring command = quoted(helper);
    command += L" ";
    command += action;
    command += L" --device ";
    command += quoted(configured_device());
    command += L" --safe-volume ";
    command += std::to_wstring(configured_safe_volume());

    std::vector<wchar_t> mutableCommand(command.begin(), command.end());
    mutableCommand.push_back(L'\0');

    STARTUPINFOW startup{};
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process{};
    const BOOL started = CreateProcessW(
        helper.c_str(),
        mutableCommand.data(),
        nullptr,
        nullptr,
        FALSE,
        CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
        nullptr,
        nullptr,
        &startup,
        &process
    );
    if (!started) {
        console::printf(
            "%s: could not start wambridge-control.exe (Windows error %lu)",
            kComponentName,
            static_cast<unsigned long>(GetLastError())
        );
        return false;
    }

    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    console::printf("%s: requested %ls", kComponentName, action);
    return true;
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
        return mainmenu_groups::playback;
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
        launch_control(item.action);
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

mainmenu_commands_factory_t<WamMenuCommands> g_wamMenuCommands;

}  // namespace
