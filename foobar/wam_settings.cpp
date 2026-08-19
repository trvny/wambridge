#include <windows.h>
#include <mmsystem.h>
#include <objidl.h>

#include <foobar2000/SDK/foobar2000.h>

#include "wam_settings.h"

#include <algorithm>
#include <array>
#include <cwchar>
#include <string>
#include <vector>

namespace wam_settings {
namespace {

constexpr wchar_t kSection[] = L"wambridge";
constexpr const wchar_t* kStreamFormats[] = {L"flac", L"wav", L"wav24", L"mp3"};

constexpr const wchar_t* kKnownIniKeys[] = {
    L"helper",
    L"device",
    L"format",
    L"volume",
    L"diagnostics",
    L"startup_silence",
    L"buffer_extra",
    L"hardware_volume",
    L"volume_max",
    L"start_volume_max",
    L"sleep_after_stop",
};

std::wstring environment_value(const wchar_t* name) {
    const DWORD needed = GetEnvironmentVariableW(name, nullptr, 0);
    if (needed == 0) return {};
    std::wstring value(needed, L'\0');
    const DWORD written = GetEnvironmentVariableW(name, value.data(), needed);
    if (written == 0 || written >= needed) return {};
    value.resize(written);
    return value;
}

std::wstring ini_value(
    const wchar_t* key,
    const wchar_t* fallback,
    const std::wstring& path
) {
    std::array<wchar_t, 32768> buffer{};
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

std::wstring raw_value(
    const std::wstring& path,
    const wchar_t* key,
    const wchar_t* environmentName,
    bool useEnvironment
) {
    if (useEnvironment) {
        const auto overridden = environment_value(environmentName);
        if (!overridden.empty()) return overridden;
    }
    return ini_value(key, L"", path);
}

std::string narrowed(const std::wstring& value) {
    std::string result;
    result.reserve(value.size());
    for (const wchar_t character : value) {
        result.push_back(
            character > 0 && character < 128 ? static_cast<char>(character) : '?'
        );
    }
    return result;
}

void report_invalid(
    bool report,
    const wchar_t* key,
    const std::wstring& value,
    const char* expected
) {
    if (!report) return;
    console::printf(
        "WAM Bridge Output: %s %s is invalid; expected %s",
        narrowed(key).c_str(),
        narrowed(value).c_str(),
        expected
    );
}

bool parse_int(
    const std::wstring& value,
    int minimum,
    int maximum,
    int fallback,
    int& output
) {
    if (value.empty()) {
        output = fallback;
        return true;
    }
    wchar_t* end = nullptr;
    const long parsed = std::wcstol(value.c_str(), &end, 10);
    if (
        end == value.c_str() ||
        *end != L'\0' ||
        parsed < minimum ||
        parsed > maximum
    ) {
        output = fallback;
        return false;
    }
    output = static_cast<int>(parsed);
    return true;
}

bool parse_optional_volume(
    const std::wstring& value,
    std::optional<int>& output
) {
    if (value.empty()) {
        output.reset();
        return true;
    }
    int parsed = 0;
    if (!parse_int(value, 0, kMaximumRawVolume, 0, parsed)) {
        output.reset();
        return false;
    }
    output = parsed;
    return true;
}

bool parse_bool(const std::wstring& value, bool& output) {
    if (value.empty() || value == L"0" || value == L"false" ||
        value == L"no" || value == L"off") {
        output = false;
        return true;
    }
    if (value == L"1" || value == L"true" || value == L"yes" ||
        value == L"on") {
        output = true;
        return true;
    }
    output = false;
    return false;
}

bool parse_format(const std::wstring& value, std::wstring& output) {
    if (value.empty()) {
        output = L"flac";
        return true;
    }
    for (const auto* format : kStreamFormats) {
        if (value == format) {
            output = value;
            return true;
        }
    }
    output = L"flac";
    return false;
}

void report_unknown_ini_keys(const std::wstring& path) {
    std::vector<wchar_t> buffer(32768);
    const DWORD size = GetPrivateProfileStringW(
        kSection,
        nullptr,
        L"",
        buffer.data(),
        static_cast<DWORD>(buffer.size()),
        path.c_str()
    );
    if (size == 0) return;

    std::wstring unknown;
    std::wstring commented;
    for (DWORD index = 0; index < size;) {
        const std::wstring key(buffer.data() + index);
        index += static_cast<DWORD>(key.size()) + 1;
        if (key.empty()) continue;
        if (key.front() == L'#') {
            if (!commented.empty()) commented += L", ";
            commented += key;
            continue;
        }

        bool known = false;
        for (const wchar_t* candidate : kKnownIniKeys) {
            if (CompareStringOrdinal(
                    key.c_str(),
                    -1,
                    candidate,
                    -1,
                    TRUE
                ) == CSTR_EQUAL) {
                known = true;
                break;
            }
        }
        if (known) continue;

        if (!unknown.empty()) unknown += L", ";
        unknown += key;
    }

    if (!unknown.empty()) {
        console::printf(
            "WAM Bridge Output: ignoring unknown setting(s) in foobar.ini: %s",
            narrowed(unknown).c_str()
        );
    }
    if (!commented.empty()) {
        console::printf(
            "WAM Bridge Output: foobar.ini key(s) start with '#': %s; "
            "Windows INI comments start with ';'",
            narrowed(commented).c_str()
        );
    }
}

Values load_values(bool useEnvironment, bool report, bool* needsNormalization) {
    const auto path = config_path();
    if (report) report_unknown_ini_keys(path);

    Values values;
    values.helper = raw_value(path, L"helper", L"WAMBRIDGE_PCM", useEnvironment);

    values.device = raw_value(path, L"device", L"WAMBRIDGE_DEVICE", useEnvironment);
    if (values.device.empty()) values.device = L"M5";

    const auto rawFormat = raw_value(
        path,
        L"format",
        L"WAMBRIDGE_FORMAT",
        useEnvironment
    );
    if (!parse_format(rawFormat, values.format)) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"format", rawFormat, "flac, wav, wav24 or mp3");
    }

    const auto rawVolume = raw_value(
        path,
        L"volume",
        L"WAMBRIDGE_VOLUME",
        useEnvironment
    );
    if (!parse_optional_volume(rawVolume, values.volume)) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"volume", rawVolume, "a raw step in 0..30");
    }

    const auto rawDiagnostics = raw_value(
        path,
        L"diagnostics",
        L"WAMBRIDGE_DIAGNOSTICS",
        useEnvironment
    );
    if (!parse_bool(rawDiagnostics, values.diagnostics)) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"diagnostics", rawDiagnostics, "0/1, false/true, no/yes or off/on");
    }

    const auto rawHardware = raw_value(
        path,
        L"hardware_volume",
        L"WAMBRIDGE_HARDWARE_VOLUME",
        useEnvironment
    );
    if (!parse_bool(rawHardware, values.hardwareVolume)) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"hardware_volume", rawHardware, "0/1, false/true, no/yes or off/on");
    }

    const auto rawMax = raw_value(
        path,
        L"volume_max",
        L"WAMBRIDGE_VOLUME_MAX",
        useEnvironment
    );
    if (!parse_int(
            rawMax,
            1,
            kMaximumRawVolume,
            kDefaultVolumeMax,
            values.volumeMax
        )) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"volume_max", rawMax, "a raw step in 1..30");
    }

    const auto rawStartMax = raw_value(
        path,
        L"start_volume_max",
        L"WAMBRIDGE_START_VOLUME_MAX",
        useEnvironment
    );
    if (!parse_int(
            rawStartMax,
            0,
            kMaximumRawVolume,
            kDefaultStartVolumeMax,
            values.startVolumeMax
        )) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"start_volume_max", rawStartMax, "a raw step in 0..30");
    }

    const auto rawSilence = raw_value(
        path,
        L"startup_silence",
        L"WAMBRIDGE_STARTUP_SILENCE",
        useEnvironment
    );
    if (!parse_int(
            rawSilence,
            0,
            kMaximumStartupSilenceMs,
            kDefaultStartupSilenceMs,
            values.startupSilenceMs
        )) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"startup_silence", rawSilence, "milliseconds in 0..10000");
    }

    const auto rawBuffer = raw_value(
        path,
        L"buffer_extra",
        L"WAMBRIDGE_BUFFER_EXTRA",
        useEnvironment
    );
    if (!parse_int(
            rawBuffer,
            0,
            kMaximumBufferExtraMs,
            kDefaultBufferExtraMs,
            values.bufferExtraMs
        )) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"buffer_extra", rawBuffer, "milliseconds in 0..10000");
    }

    const auto rawSleep = raw_value(
        path,
        L"sleep_after_stop",
        L"WAMBRIDGE_SLEEP_AFTER_STOP",
        useEnvironment
    );
    if (!parse_int(
            rawSleep,
            0,
            kMaximumSleepAfterStopSeconds,
            kDefaultSleepAfterStopSeconds,
            values.sleepAfterStopSeconds
        )) {
        if (needsNormalization != nullptr) *needsNormalization = true;
        report_invalid(report, L"sleep_after_stop", rawSleep, "seconds in 0..86400");
    }

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

bool ensure_unicode_config_file() {
    if (!ensure_config_directory()) return false;
    const auto path = config_path();
    HANDLE file = CreateFileW(
        path.c_str(),
        GENERIC_WRITE,
        FILE_SHARE_READ,
        nullptr,
        CREATE_NEW,
        FILE_ATTRIBUTE_NORMAL,
        nullptr
    );
    if (file == INVALID_HANDLE_VALUE) {
        const DWORD error = GetLastError();
        return error == ERROR_FILE_EXISTS || error == ERROR_ALREADY_EXISTS;
    }

    constexpr BYTE bom[] = {0xFF, 0xFE};
    DWORD written = 0;
    const bool ok = WriteFile(file, bom, sizeof(bom), &written, nullptr) != FALSE &&
        written == sizeof(bom);
    CloseHandle(file);
    if (!ok) DeleteFileW(path.c_str());
    return ok;
}

bool write_setting(
    const std::wstring& path,
    const wchar_t* key,
    const std::wstring& value,
    const std::wstring& defaultValue
) {
    const wchar_t* stored = value == defaultValue ? nullptr : value.c_str();
    return WritePrivateProfileStringW(kSection, key, stored, path.c_str()) != FALSE;
}

}  // namespace

Values default_values() {
    return {};
}

IniLoadResult load_ini_values() {
    IniLoadResult result;
    result.values = load_values(false, false, &result.needsNormalization);
    return result;
}

Values load_effective_values() {
    return load_values(true, true, nullptr);
}

bool equal_values(const Values& left, const Values& right) {
    return left.helper == right.helper &&
        left.device == right.device &&
        left.format == right.format &&
        left.volume == right.volume &&
        left.diagnostics == right.diagnostics &&
        left.startupSilenceMs == right.startupSilenceMs &&
        left.bufferExtraMs == right.bufferExtraMs &&
        left.hardwareVolume == right.hardwareVolume &&
        left.volumeMax == right.volumeMax &&
        left.startVolumeMax == right.startVolumeMax &&
        left.sleepAfterStopSeconds == right.sleepAfterStopSeconds;
}

std::wstring config_path() {
    const auto localAppData = environment_value(L"LOCALAPPDATA");
    if (localAppData.empty()) return L"foobar.ini";
    return localAppData + L"\\WAMBridge\\foobar.ini";
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

int clamped_int(
    const std::wstring& value,
    int fallback,
    int minimum,
    int maximum
) {
    if (value.empty()) return fallback;
    wchar_t* end = nullptr;
    const long parsed = std::wcstol(value.c_str(), &end, 10);
    if (end == value.c_str() || *end != L'\0') return fallback;
    const long bounded = (std::max)(
        static_cast<long>(minimum),
        (std::min)(static_cast<long>(maximum), parsed)
    );
    return static_cast<int>(bounded);
}

bool write_values(const Values& values) {
    if (!ensure_unicode_config_file()) return false;
    const auto path = config_path();
    const auto defaultVolumeMax = std::to_wstring(kDefaultVolumeMax);
    const auto defaultStartVolumeMax = std::to_wstring(kDefaultStartVolumeMax);
    const auto defaultStartupSilence = std::to_wstring(kDefaultStartupSilenceMs);
    const auto defaultBufferExtra = std::to_wstring(kDefaultBufferExtraMs);
    const auto defaultSleepAfterStop = std::to_wstring(kDefaultSleepAfterStopSeconds);
    const auto volume = values.volume.has_value()
        ? std::to_wstring(*values.volume)
        : std::wstring();

    bool ok = true;
    ok = write_setting(path, L"device", values.device, L"M5") && ok;
    ok = write_setting(path, L"format", values.format, L"flac") && ok;
    ok = write_setting(path, L"volume", volume, L"") && ok;
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
        defaultVolumeMax
    ) && ok;
    ok = write_setting(
        path,
        L"start_volume_max",
        std::to_wstring(values.startVolumeMax),
        defaultStartVolumeMax
    ) && ok;
    ok = write_setting(
        path,
        L"startup_silence",
        std::to_wstring(values.startupSilenceMs),
        defaultStartupSilence
    ) && ok;
    ok = write_setting(
        path,
        L"buffer_extra",
        std::to_wstring(values.bufferExtraMs),
        defaultBufferExtra
    ) && ok;
    ok = write_setting(
        path,
        L"sleep_after_stop",
        std::to_wstring(values.sleepAfterStopSeconds),
        defaultSleepAfterStop
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

}  // namespace wam_settings
