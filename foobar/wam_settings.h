#pragma once

#include <optional>
#include <string>

namespace wam_settings {

inline constexpr int kMaximumRawVolume = 30;
inline constexpr int kDefaultVolumeMax = 10;
inline constexpr int kDefaultStartVolumeMax = 3;
inline constexpr int kDefaultStartupSilenceMs = 0;
inline constexpr int kMaximumStartupSilenceMs = 10000;
inline constexpr int kDefaultBufferExtraMs = 0;
inline constexpr int kMaximumBufferExtraMs = 10000;
inline constexpr int kDefaultSleepAfterStopSeconds = 0;
inline constexpr int kMaximumSleepAfterStopSeconds = 86400;

struct Values {
    std::wstring helper;
    std::wstring device = L"M5";
    std::wstring format = L"flac";
    std::optional<int> volume;
    bool diagnostics = false;
    int startupSilenceMs = kDefaultStartupSilenceMs;
    int bufferExtraMs = kDefaultBufferExtraMs;
    bool hardwareVolume = false;
    int volumeMax = kDefaultVolumeMax;
    int startVolumeMax = kDefaultStartVolumeMax;
    int sleepAfterStopSeconds = kDefaultSleepAfterStopSeconds;
};

struct IniLoadResult {
    Values values;
    bool needsNormalization = false;
};

Values default_values();
IniLoadResult load_ini_values();
Values load_effective_values();
bool write_values(const Values& values);
bool equal_values(const Values& left, const Values& right);
bool has_environment_overrides();
std::wstring config_path();
int clamped_int(
    const std::wstring& value,
    int fallback,
    int minimum,
    int maximum
);

}  // namespace wam_settings
