// winsock2 before windows.h, or windows.h pulls in the 1.1 headers and the
// two sets of declarations collide.
#include <winsock2.h>
#include <ws2tcpip.h>

#include <windows.h>
#include <mmsystem.h>
#include <objidl.h>

#include <foobar2000/SDK/foobar2000.h>

#include "wam_control.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cwchar>
#include <deque>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

namespace {

constexpr char kComponentName[] = "WAM Bridge Output";
constexpr char kOutputName[] = "WAM Bridge";
constexpr char kDeviceName[] = "Samsung M5 (Wi-Fi)";
constexpr size_t kWriteBatchFrames = 4096;
// The M5's measured raw scale. Step 30 is its maximum and is very loud, so the
// slider maps onto 0..volume_max rather than onto the whole range: a fresh
// foobar sits at 0 dB, and that must not mean "as loud as the speaker goes".
// Also used to lift the helper's start-volume clamp when a helper is being
// replaced mid-session rather than started.
constexpr int kMaximumRawVolume = 30;
constexpr int kDefaultVolumeMax = 10;
// The highest raw step the first helper of a playback session may start at,
// however high the slider is sitting. The slider governs everything after it.
//
// The curve itself is fine, which is why this is a cap and not a different
// mapping. Measured by ear on the M5 on 2026-08-15 over a slider mapped onto
// 0..10: 1 inaudible, 2 barely there, 3 a little more, 4 clearly louder, 5
// distinct, 6 enough to cut through conversation in the room, 7 comfortable
// listening. No cliff anywhere in it.
//
// What made the owner jump was never the mapping. Arriving at 7 by dragging the
// slider sounds fine; arriving at 7 cold, on a track whose loudness nobody knows
// yet, does not - and a slider left there after an evening of listening is
// exactly what the next session started from. So the start is capped and the
// listener raises it, which is the shape they asked for: "not a global limit,
// just don't scare me every single time".
constexpr int kDefaultStartVolumeMax = 3;
// Below this the slider is treated as silence. Amplitude at -60 dB is 0.001,
// which rounds to step 0 for every ceiling in range anyway.
constexpr double kSilenceDecibels = -60.0;
// One counter line per second, long enough to cover a whole track. The clock
// terms are the only way to tell which one runs away; a physical run measured
// foobar advancing at a median 11x with no term ever observed.
constexpr unsigned kMaxCounterLines = 240;
constexpr std::chrono::milliseconds kCounterInterval{1000};
constexpr std::chrono::milliseconds kAcceptWaitSlice{50};
constexpr std::chrono::milliseconds kFlushGrace{2000};
constexpr DWORD kActiveShutdownGraceMs = 2000;
constexpr DWORD kStartupShutdownGraceMs = 25000;

// {B768F82C-A6B7-436F-965D-6C8D1B21B91D}
constexpr GUID kOutputGuid = {
    0xb768f82c,
    0xa6b7,
    0x436f,
    {0x96, 0x5d, 0x6c, 0x8d, 0x1b, 0x21, 0xb9, 0x1d},
};

// {C51F799E-CB6E-469E-A7B4-FD0137CD4B4B}
constexpr GUID kDeviceGuid = {
    0xc51f799e,
    0xcb6e,
    0x469e,
    {0xa7, 0xb4, 0xfd, 0x01, 0x37, 0xcd, 0x4b, 0x4b},
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

std::wstring config_path() {
    const auto localAppData = environment_value(L"LOCALAPPDATA");
    if (localAppData.empty()) return L"foobar.ini";
    return localAppData + L"\\WAMBridge\\foobar.ini";
}

bool file_exists(const std::wstring& path) {
    const DWORD attributes = GetFileAttributesW(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES &&
        (attributes & FILE_ATTRIBUTE_DIRECTORY) == 0;
}

// Always an absolute path next to the component, never a bare file name: a
// bare name makes CreateProcessW search the working directory first, so any
// writable directory foobar happens to be started from could supply the
// helper. An empty result fails the launch with the configuration message.
std::wstring bundled_helper_path() {
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&kOutputGuid),
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

    std::wstring modulePath(buffer.data(), size);
    const size_t separator = modulePath.find_last_of(L"\\/");
    if (separator == std::wstring::npos) return {};

    return modulePath.substr(0, separator) +
        L"\\wambridge-pcm\\wambridge-pcm.exe";
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

// Key names are ASCII by construction; anything else in the file is a typo and
// only has to survive as far as the console line that reports it.
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

// Formats the helper accepts. Anything else would reach its CLI as a rejected
// argument and take the whole stream down with it.
constexpr const wchar_t* kStreamFormats[] = {L"flac", L"wav", L"wav24", L"mp3"};
constexpr const wchar_t* kDefaultStreamFormat = L"flac";

// Milliseconds of silence FFmpeg prepends to the stream. Straight added delay
// on a path about 6 s long; kept configurable so the hardware can say whether
// it is still load-bearing. Measured at 0 on 2026-08-08: startup still reaches
// WAMBRIDGE PLAYING.
constexpr int kDefaultStartupSilenceMs = 1500;
constexpr int kMaximumStartupSilenceMs = 10000;

// Milliseconds of queue this output keeps on top of foobar's own buffer length.
// Measured 2026-08-08: the queue runs almost exactly full - 3.79 to 3.99 s of a
// 4.0 s capacity - so every millisecond of capacity is a millisecond of delay,
// and this term plus the 2 s clamp floor below it is the largest single share
// of the roughly six seconds that reach the ear. It was chosen, never measured.
// Configurable so the hardware can say where the pipe starts to starve; the
// default deliberately keeps today's behaviour until it has.
constexpr int kDefaultBufferExtraMs = 2000;
constexpr int kMaximumBufferExtraMs = 10000;

// Every key this component reads. A file may legitimately outlive the build that
// understood it -- `hardware_volume` exists only on an unmerged branch -- and an
// ignored key is indistinguishable from a working one from the outside.
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
};

void report_unknown_ini_keys(const std::wstring& path) {
    // A null key name asks for the section's key names as a double-null
    // terminated block, which is the only way to see what the file actually has.
    std::vector<wchar_t> buffer(32768);
    const DWORD size = GetPrivateProfileStringW(
        L"wambridge",
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
        // Windows comments start with `;` and those never reach this loop,
        // because the profile API drops them. `#` is an ordinary character to
        // it, so `#format=flac` arrives as a key literally named `#format`.
        // Reported separately rather than skipped: a line someone believes is
        // disabled is exactly the confusion this function exists to remove, and
        // `#hardware_volume=1` is a real setting nobody is applying.
        if (key.front() == L'#') {
            if (!commented.empty()) commented += L", ";
            commented += key;
            continue;
        }

        // Case-insensitively: GetPrivateProfileStringW finds `Device=M5` when
        // asked for `device`, so an exact comparison would announce a setting
        // as ignored while it was being applied. Reporting a working key as
        // dead is the same failure this function exists to remove, pointed the
        // other way.
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
            }
        }
        if (known) continue;

        if (!unknown.empty()) unknown += L", ";
        unknown += key;
    }
    // Only %u and %s: console::printf is pfc's formatter, not the CRT one.
    if (!unknown.empty()) {
        console::printf(
            "%s: ignoring unknown setting(s) in foobar.ini: %s",
            kComponentName,
            narrowed(unknown).c_str()
        );
    }
    if (!commented.empty()) {
        console::printf(
            "%s: foobar.ini has setting(s) starting with '#': %s. Windows "
            "comments start with ';', so these are names, not disabled lines, "
            "and nothing reads them",
            kComponentName,
            narrowed(commented).c_str()
        );
    }
}

struct Settings {
    std::wstring helper;
    std::wstring device;
    std::wstring format;
    std::optional<int> volume;
    bool diagnostics = false;
    int startupSilenceMs = kDefaultStartupSilenceMs;
    int bufferExtraMs = kDefaultBufferExtraMs;
    bool hardwareVolume = false;
    int volumeMax = kDefaultVolumeMax;
    int startVolumeMax = kDefaultStartVolumeMax;
};

bool truthy(const std::wstring& value) {
    return value == L"1" || value == L"true" || value == L"yes" ||
        value == L"on";
}

Settings load_settings() {
    const auto path = config_path();
    report_unknown_ini_keys(path);
    auto helper = environment_value(L"WAMBRIDGE_PCM");
    if (helper.empty()) {
        const auto configured = ini_value(L"helper", L"", path);
        if (!configured.empty() && !file_exists(configured)) {
            // Otherwise a developer measures the bundled binary while believing
            // a custom build is under test, which makes the numbers describe
            // something nobody chose. The artifact has to stay identifiable.
            console::printf(
                "%s: helper %s does not exist, using the bundled one",
                kComponentName,
                narrowed(configured).c_str()
            );
        }
        helper = configured.empty() || !file_exists(configured)
            ? bundled_helper_path()
            : configured;
    }

    auto device = environment_value(L"WAMBRIDGE_DEVICE");
    if (device.empty()) device = ini_value(L"device", L"M5", path);

    // FLAC unless asked otherwise. The prebuffer is partly bounded by bytes:
    // mp3 at 320 kbps measured 16.9 s against FLAC's 13.4 s, because a thinner
    // stream fits more seconds into the same space. wav pulls the same lever
    // the other way and has not been heard on hardware yet.
    auto format = environment_value(L"WAMBRIDGE_FORMAT");
    if (format.empty()) format = ini_value(L"format", L"", path);
    bool known = false;
    for (const wchar_t* candidate : kStreamFormats) {
        if (format == candidate) known = true;
    }
    if (!known) {
        // Falling back silently is how a typo becomes "wav did not help".
        if (!format.empty()) {
            console::printf(
                "%s: unknown format %s, falling back to %s",
                kComponentName,
                narrowed(format).c_str(),
                narrowed(kDefaultStreamFormat).c_str()
            );
        }
        format = kDefaultStreamFormat;
    }

    std::optional<int> volume;
    auto rawVolume = environment_value(L"WAMBRIDGE_VOLUME");
    if (rawVolume.empty()) rawVolume = ini_value(L"volume", L"", path);
    if (!rawVolume.empty()) {
        wchar_t* end = nullptr;
        const long parsed = std::wcstol(rawVolume.c_str(), &end, 10);
        if (end != rawVolume.c_str() && *end == L'\0' && parsed >= 0 && parsed <= 100) {
            volume = static_cast<int>(parsed);
        } else {
            // Same silence as the two above: without this the speaker simply
            // starts wherever it was, and the file looks like it asked for
            // something else.
            console::printf(
                "%s: volume %s is not a number in 0..100, leaving the "
                "speaker's own level",
                kComponentName,
                narrowed(rawVolume).c_str()
            );
        }
    }
    // Off unless asked for: the clock counters are a diagnostic, and a normal
    // session should not push 240 lines into the user's console.
    auto rawDiagnostics = environment_value(L"WAMBRIDGE_DIAGNOSTICS");
    if (rawDiagnostics.empty()) {
        rawDiagnostics = ini_value(L"diagnostics", L"", path);
    }
    const bool diagnostics = truthy(rawDiagnostics);

    // Off unless asked for as well. The host gain is heard about thirteen
    // seconds late, but it is also the only volume that works when the speaker
    // is unreachable, so switching the slider over stays a deliberate choice.
    auto rawHardware = environment_value(L"WAMBRIDGE_HARDWARE_VOLUME");
    if (rawHardware.empty()) {
        rawHardware = ini_value(L"hardware_volume", L"", path);
    }
    const bool hardwareVolume = truthy(rawHardware);

    int volumeMax = kDefaultVolumeMax;
    auto rawMax = environment_value(L"WAMBRIDGE_VOLUME_MAX");
    if (rawMax.empty()) rawMax = ini_value(L"volume_max", L"", path);
    if (!rawMax.empty()) {
        wchar_t* end = nullptr;
        const long parsed = std::wcstol(rawMax.c_str(), &end, 10);
        if (end != rawMax.c_str() && *end == L'\0' && parsed >= 1 &&
            parsed <= kMaximumRawVolume) {
            volumeMax = static_cast<int>(parsed);
        }
    }

    int startVolumeMax = kDefaultStartVolumeMax;
    auto rawStartMax = environment_value(L"WAMBRIDGE_START_VOLUME_MAX");
    if (rawStartMax.empty()) {
        rawStartMax = ini_value(L"start_volume_max", L"", path);
    }
    if (!rawStartMax.empty()) {
        wchar_t* end = nullptr;
        const long parsed = std::wcstol(rawStartMax.c_str(), &end, 10);
        // Zero is a real answer here and means "no cap, start where the slider
        // points", so the range starts at 0 rather than 1.
        if (end != rawStartMax.c_str() && *end == L'\0' && parsed >= 0 &&
            parsed <= kMaximumRawVolume) {
            startVolumeMax = static_cast<int>(parsed);
        } else {
            console::printf(
                "%s: start_volume_max %s is not a raw step in 0..%u, keeping %u",
                kComponentName,
                narrowed(rawStartMax).c_str(),
                static_cast<unsigned>(kMaximumRawVolume),
                static_cast<unsigned>(startVolumeMax)
            );
        }
    }

    int startupSilenceMs = kDefaultStartupSilenceMs;
    auto rawSilence = environment_value(L"WAMBRIDGE_STARTUP_SILENCE");
    if (rawSilence.empty()) {
        rawSilence = ini_value(L"startup_silence", L"", path);
    }
    if (!rawSilence.empty()) {
        wchar_t* end = nullptr;
        const long parsed = std::wcstol(rawSilence.c_str(), &end, 10);
        if (end != rawSilence.c_str() && *end == L'\0' && parsed >= 0 &&
            parsed <= kMaximumStartupSilenceMs) {
            startupSilenceMs = static_cast<int>(parsed);
        } else {
            console::printf(
                "%s: startup_silence %s is not a number in 0..%u, using %u ms",
                kComponentName,
                narrowed(rawSilence).c_str(),
                static_cast<unsigned>(kMaximumStartupSilenceMs),
                static_cast<unsigned>(kDefaultStartupSilenceMs)
            );
        }
    }

    int bufferExtraMs = kDefaultBufferExtraMs;
    auto rawBufferExtra = environment_value(L"WAMBRIDGE_BUFFER_EXTRA");
    if (rawBufferExtra.empty()) {
        rawBufferExtra = ini_value(L"buffer_extra", L"", path);
    }
    if (!rawBufferExtra.empty()) {
        wchar_t* end = nullptr;
        const long parsed = std::wcstol(rawBufferExtra.c_str(), &end, 10);
        if (end != rawBufferExtra.c_str() && *end == L'\0' && parsed >= 0 &&
            parsed <= kMaximumBufferExtraMs) {
            bufferExtraMs = static_cast<int>(parsed);
        } else {
            // This knob exists to be walked down during a measurement, so a
            // typo would otherwise read as "that value changed nothing".
            console::printf(
                "%s: buffer_extra %s is not a number in 0..%u, using %u ms",
                kComponentName,
                narrowed(rawBufferExtra).c_str(),
                static_cast<unsigned>(kMaximumBufferExtraMs),
                static_cast<unsigned>(kDefaultBufferExtraMs)
            );
        }
    }

    return {
        std::move(helper),
        std::move(device),
        std::move(format),
        volume,
        diagnostics,
        startupSilenceMs,
        bufferExtraMs,
        hardwareVolume,
        volumeMax,
        startVolumeMax,
    };
}

// The one socket the component keeps to a running helper's control listener.
//
// Namespace scope because the slider dispatcher lives in wam_menu.cpp, a
// separate translation unit, and reaches it through wam_control.h. Guarded
// rather than atomic: connecting, sending and closing must not interleave.
std::mutex g_controlMutex;
SOCKET g_controlSocket = INVALID_SOCKET;

// Read by the menu dispatcher through wam_control.h, which is a different
// translation unit and has no access to the output's settings.
std::atomic<bool> g_hardwareVolume{false};
std::atomic<int> g_volumeMax{kDefaultVolumeMax};

// Moves foobar's own slider. playback_control is a main-thread interface and
// the dispatcher runs on its own thread, so the change is handed over rather
// than made where it was decided.
class SliderSync : public main_thread_callback {
public:
    explicit SliderSync(double decibels) : m_decibels(decibels) {}

    void callback_run() override {
        playback_control::get()->set_volume(static_cast<float>(m_decibels));
    }

private:
    double m_decibels;
};

void close_control_socket_locked() {
    if (g_controlSocket == INVALID_SOCKET) return;
    shutdown(g_controlSocket, SD_BOTH);
    closesocket(g_controlSocket);
    g_controlSocket = INVALID_SOCKET;
}

// Connect to a loopback listener the helper announced, hand over its token and
// keep the socket. Returns quietly on failure: a slider that cannot reach the
// helper falls back to the control process, which is what it did before.
bool open_control_socket(unsigned short port, const std::string& token) {
    WSADATA data{};
    // Reference counted per process, so calling it here is safe even though
    // foobar has certainly started Winsock already.
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) return false;

    SOCKET handle = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (handle == INVALID_SOCKET) return false;

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(handle, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) {
        closesocket(handle);
        return false;
    }

    // A blocked send must not hold the slider thread: the level is already
    // stale by the time anything is that slow.
    DWORD timeout = 1000;
    setsockopt(
        handle,
        SOL_SOCKET,
        SO_SNDTIMEO,
        reinterpret_cast<const char*>(&timeout),
        sizeof(timeout)
    );

    const std::string greeting = token + "\n";
    if (send(handle, greeting.c_str(), static_cast<int>(greeting.size()), 0) !=
        static_cast<int>(greeting.size())) {
        closesocket(handle);
        return false;
    }

    std::lock_guard lock(g_controlMutex);
    close_control_socket_locked();
    g_controlSocket = handle;
    return true;
}

void close_handle(HANDLE& handle) {
    if (handle != nullptr && handle != INVALID_HANDLE_VALUE) {
        CloseHandle(handle);
        handle = nullptr;
    }
}

class WamOutput : public output_v6 {
public:
    WamOutput(const GUID&, double bufferLength, bool, t_uint32)
        : m_bufferLength(std::clamp(bufferLength, 2.0, 30.0)),
          m_settings(load_settings()),
          m_worker(&WamOutput::worker_loop, this) {
        // Published for the menu dispatcher, which lives in another translation
        // unit and has to know whether moving the slider is its business.
        g_hardwareVolume.store(m_settings.hardwareVolume);
        g_volumeMax.store(m_settings.volumeMax);
    }

    ~WamOutput() {
        {
            std::lock_guard lock(m_mutex);
            m_shutdown = true;
            m_childStopping.store(true);
        }
        cancel_child();
        m_cv.notify_all();
        if (m_worker.joinable()) m_worker.join();
        stop_child();
    }

    static void g_enum_devices(output_device_enum_callback& callback) {
        callback.on_device(
            kDeviceGuid,
            kDeviceName,
            static_cast<unsigned>(sizeof(kDeviceName) - 1)
        );
    }

    static GUID g_get_guid() { return kOutputGuid; }
    static const char* g_get_name() { return kOutputName; }
    static bool g_advanced_settings_query() { return false; }
    static bool g_needs_bitdepth_config() { return false; }
    static bool g_needs_dither_config() { return false; }
    static bool g_needs_device_list_prefixes() { return false; }
    static bool g_supports_multiple_streams() { return false; }
    static bool g_is_high_latency() { return true; }
    static uint32_t g_extra_flags() { return output_entry::flag_needs_shims; }

    double get_latency() override {
        std::lock_guard lock(m_mutex);
        if (m_flushing || m_sampleRate == 0 || m_channels == 0) return 0.0;
        const auto now = std::chrono::steady_clock::now();
        refresh_playback_clock_locked(now);
        const uint64_t frames = buffered_frames_locked() +
            startup_delay_frames_locked(now);
        return static_cast<double>(frames) / m_sampleRate;
    }

    void process_samples(const audio_chunk& chunk) override {
        // This entry point returns void, so a partial write cannot be
        // reported and the caller counts the whole chunk as delivered.
        // Taking only what fits therefore dropped the rest: measured on a
        // physical M5 as foobar advancing at ~11x while the pipe stayed at
        // 1.0x and free space hovered around 100 ms. Block until the chunk
        // is in, and only give up when the stream is going away anyway.
        const size_t frames = chunk.get_sample_count();
        if (frames == 0 || chunk.get_channels() == 0) return;

        uint64_t generation = 0;
        {
            std::lock_guard lock(m_mutex);
            generation = m_generation;
        }

        size_t offset = 0;
        while (offset < frames) {
            const size_t taken = submit_chunk(chunk, offset);
            if (taken > 0) {
                offset += taken;
                continue;
            }
            if (!wait_for_room(generation)) return;
        }
    }

    size_t process_samples_v2(const audio_chunk& chunk) override {
        return submit_chunk(chunk, 0);
    }

    // Returns false once waiting is pointless: the stream is being torn down,
    // replaced or shut down, and dropping the remainder is then correct.
    bool wait_for_room(uint64_t generation) {
        std::unique_lock lock(m_mutex);
        throw_if_failed_locked();
        if (m_shutdown || m_flushing || generation != m_generation) return false;
        m_cv.wait_for(lock, kAcceptWaitSlice);
        throw_if_failed_locked();
        return !(m_shutdown || m_flushing || generation != m_generation);
    }

    size_t submit_chunk(const audio_chunk& chunk, size_t offset) {
        const size_t total = chunk.get_sample_count();
        if (total == 0 || chunk.get_channels() == 0 || offset >= total) return 0;

        std::unique_lock lock(m_mutex);
        throw_if_failed_locked();
        if (m_paused.load()) return 0;

        const unsigned sampleRate = chunk.get_sample_rate();
        const unsigned channels = chunk.get_channels();
        if (sampleRate != m_sampleRate || channels != m_channels) {
            m_queue.clear();
            reset_clock_locked();
            m_sampleRate = sampleRate;
            m_channels = channels;
            // Capacity is delay: the queue measured 3.79-3.99 s full of a 4.0 s
            // capacity, so whatever is allowed here is heard that much later.
            m_capacityFrames = static_cast<size_t>(
                std::ceil(
                    (m_bufferLength + m_settings.bufferExtraMs / 1000.0) *
                    static_cast<double>(m_sampleRate)
                )
            );
            m_flushing = false;
            ++m_generation;
            m_restart = true;
            m_helperReady.store(false);
            m_playing.store(false);
            m_childStopping.store(true);
            cancel_child();
        }

        refresh_playback_clock_locked(std::chrono::steady_clock::now());
        const size_t freeFrames = free_frames_locked();
        const size_t takenFrames = std::min<size_t>(freeFrames, total - offset);
        if (takenFrames == 0) return 0;
        // Count the offer once the first slice of it is in, so a retry cannot
        // count it twice and a format change cannot reset the counter after
        // it was already added. A radio switch to 48 kHz left offered a whole
        // chunk behind submitted until this moved here.
        if (offset == 0) m_offeredFrames += total;
        m_flushing = false;
        m_drainRequested = false;
        if (m_clockStarted && m_helperReady.load()) {
            m_playing.store(true);
        }

        const audio_sample* input = chunk.get_data() + offset * channels;
        const size_t values = takenFrames * channels;
        for (size_t index = 0; index < values; ++index) {
            m_queue.push_back(static_cast<float>(input[index]));
        }

        lock.unlock();
        m_cv.notify_all();
        return takenFrames;
    }

    void update(bool& ready) override {
        ready = update_v2() != 0;
    }

    size_t update_v2() override {
        std::lock_guard lock(m_mutex);
        throw_if_failed_locked();
        const auto now = std::chrono::steady_clock::now();
        refresh_playback_clock_locked(now);
        finish_playback_clock_if_drained_locked();
        log_counters_locked(now);
        if (m_paused.load()) return 0;
        if (m_sampleRate == 0 || m_channels == 0) return SIZE_MAX;
        return free_frames_locked();
    }

    bool is_progressing() override {
        std::lock_guard lock(m_mutex);
        refresh_playback_clock_locked(std::chrono::steady_clock::now());
        finish_playback_clock_if_drained_locked();
        return m_playing.load() && !m_paused.load() && !m_flushing;
    }

    void pause(bool state) override {
        {
            std::lock_guard lock(m_mutex);
            const bool wasPaused = m_paused.load();
            if (state == wasPaused) return;

            const auto now = std::chrono::steady_clock::now();
            refresh_playback_clock_locked(now);
            if (state) {
                m_pauseStarted = now;
            } else {
                if (m_clockStarted &&
                    m_pauseStarted != std::chrono::steady_clock::time_point{}) {
                    m_clockAnchor += now - m_pauseStarted;
                }
                m_pauseStarted = {};
            }
            m_paused.store(state);
        }
        m_cv.notify_all();
    }

    void flush() override {
        {
            std::lock_guard lock(m_mutex);
            m_queue.clear();
            m_failure.clear();
            retire_stream_locked();
            m_restart = true;
            cancel_child();
        }
        m_cv.notify_all();
    }

    void force_play() override {
        {
            std::lock_guard lock(m_mutex);
            m_drainRequested = true;
            finish_playback_clock_if_drained_locked();
        }
        m_cv.notify_all();
    }

    void volume_set(double decibels) override {
        if (!m_settings.hardwareVolume) {
            m_gain.store(std::pow(10.0, decibels / 20.0));
            return;
        }
        // Applying both would attenuate twice. The host gain is the one that
        // arrives about thirteen seconds late, so it is the one that goes.
        m_gain.store(1.0);
        const int step = volume_step_for(decibels, m_settings.volumeMax);
        m_lastVolumeStep.store(step);
        wam::request_volume_step(step);
    }

    // foobar hands out dB, the M5 takes raw steps, and the slider has to feel
    // even across its travel.
    //
    // This was linear in amplitude first, to match the host gain it replaces.
    // On hardware that put four fifths of the slider into silence: at -20 dB
    // the amplitude is 0.1, which against a ceiling of 10 is step 1, and
    // everything below it is step 0. Eighty decibels of travel on two steps.
    //
    // Linear in decibels instead, over the usable range. That is how a volume
    // control is normally scaled and it spreads the ceiling across the whole
    // slider rather than the top few dB.
    //
    // Whether the M5's own steps are even in dB is still NOT measured. If they
    // turn out not to be, this needs the speaker's curve, not a different line.
    static int volume_step_for(double decibels, int ceiling) {
        if (decibels <= kSilenceDecibels) return 0;
        const double span = -kSilenceDecibels;
        const double fraction = (decibels - kSilenceDecibels) / span;
        const long step = std::lround(fraction * static_cast<double>(ceiling));
        // Above the silence floor the slider is asking for something audible,
        // so it never rounds back down to a muted speaker.
        return static_cast<int>(std::max<long>(1, std::min<long>(ceiling, step)));
    }

    // The inverse, for putting the slider where a menu action left the speaker.
    // Step 0 is the floor rather than foobar's -100 dB: the slider only has to
    // agree about what the speaker is doing, not reproduce its silence exactly.
    static double decibels_for_step(int step, int ceiling) {
        if (ceiling <= 0 || step <= 0) return kSilenceDecibels;
        const double fraction =
            static_cast<double>(std::min(step, ceiling)) / static_cast<double>(ceiling);
        return kSilenceDecibels + fraction * -kSilenceDecibels;
    }

private:
    enum class ChildState {
        none,
        running,
        exited,
    };

    // "<port> <token>" from WAMBRIDGE CONTROL_PORT. A helper that is no longer
    // the current one must not take over the socket, hence the generation check.
    void connect_control_channel(const std::string& arguments, uint64_t generation) {
        {
            std::lock_guard lock(m_mutex);
            if (generation != m_generation || m_shutdown) return;
        }
        const size_t space = arguments.find(' ');
        if (space == std::string::npos) return;

        const std::string portText = arguments.substr(0, space);
        const std::string token = arguments.substr(space + 1);
        if (token.empty()) return;

        unsigned long port = 0;
        try {
            port = std::stoul(portText);
        } catch (const std::exception&) {
            return;
        }
        if (port == 0 || port > 65535) return;

        if (!open_control_socket(static_cast<unsigned short>(port), token)) {
            // Only %u and %s: console::printf is pfc's formatter.
            console::printf(
                "%s: could not reach the helper's control channel on port %u; "
                "volume falls back to a control process",
                kComponentName,
                static_cast<unsigned>(port)
            );
        }
    }

    void retire_stream_locked() {
        m_queue.clear();
        m_sampleRate = 0;
        m_channels = 0;
        m_capacityFrames = 0;
        m_flushing = false;
        reset_clock_locked();
        ++m_generation;
        m_helperReady.store(false);
        m_playing.store(false);
        m_childStopping.store(true);
    }

    size_t queued_frames_locked() const {
        return m_channels == 0 ? 0 : m_queue.size() / m_channels;
    }

    uint64_t buffered_frames_locked() const {
        const uint64_t submitted = m_submittedFrames >= m_playedFrames
            ? m_submittedFrames - m_playedFrames
            : 0;
        return static_cast<uint64_t>(queued_frames_locked()) +
            m_writeInProgressFrames + submitted;
    }

    size_t free_frames_locked() const {
        const uint64_t buffered = buffered_frames_locked();
        return buffered >= m_capacityFrames
            ? 0
            : m_capacityFrames - static_cast<size_t>(buffered);
    }

    void reset_clock_locked() {
        m_writeInProgressFrames = 0;
        m_submittedFrames = 0;
        m_playedFrames = 0;
        m_clockAnchorFrames = 0;
        m_clockTargetFrames = 0;
        m_offeredFrames = 0;
        m_clockAnchor = {};
        m_pauseStarted = {};
        m_clockStarted = false;
        m_drainRequested = false;
        m_childExited = false;
    }

    void start_playback_clock_locked(
        std::chrono::steady_clock::time_point now
    ) {
        if (m_clockStarted) return;
        m_clockStarted = true;
        m_clockAnchorFrames = m_playedFrames;
        // The clock must hold back exactly as long as the silence FFmpeg is
        // prepending, because that silence is what the speaker plays first.
        // Hardcoding 1.5 s here while the helper is told something else leaves
        // a phantom delay at 0 and marks frames played under real audio at
        // larger values, which skews latency, capacity and track transitions.
        m_clockAnchor = now + startup_silence_duration();
        if (m_paused.load()) m_pauseStarted = now;
    }

    void refresh_playback_clock_locked(
        std::chrono::steady_clock::time_point now
    ) {
        if (!m_clockStarted || m_paused.load() || m_sampleRate == 0 ||
            now <= m_clockAnchor) {
            return;
        }

        const auto elapsed = std::chrono::duration<double>(now - m_clockAnchor);
        const uint64_t elapsedFrames = static_cast<uint64_t>(
            elapsed.count() * static_cast<double>(m_sampleRate)
        );
        const uint64_t target = m_clockAnchorFrames + elapsedFrames;
        m_clockTargetFrames = target;
        m_playedFrames = std::min(target, m_submittedFrames);
    }

    unsigned frames_to_ms_locked(uint64_t frames) const {
        if (m_sampleRate == 0) return 0;
        return static_cast<unsigned>(frames * 1000ull / m_sampleRate);
    }

    void log_counters_locked(std::chrono::steady_clock::time_point now) {
        if (!m_settings.diagnostics) return;
        if (m_sampleRate == 0 || !m_clockStarted) return;
        if (m_counterLines >= kMaxCounterLines) return;
        if (m_lastCounterLog != std::chrono::steady_clock::time_point{} &&
            now - m_lastCounterLog < kCounterInterval) {
            return;
        }
        m_lastCounterLog = now;
        ++m_counterLines;

        std::string flags;
        if (m_playing.load()) flags += 'P';
        if (m_paused.load()) flags += 'p';
        if (m_flushing) flags += 'f';
        if (m_drainRequested) flags += 'd';
        if (m_helperReady.load()) flags += 'R';
        if (flags.empty()) flags = "-";

        // Only %u and %s: console::printf is pfc's formatter and prints the
        // length modifiers in %lu and %llu literally.
        console::printf(
            "%s: CLOCK target=%ums offered=%ums submitted=%ums played=%ums "
            "queued=%ums write=%ums buffered=%ums free=%ums capacity=%ums "
            "flags=%s",
            kComponentName,
            frames_to_ms_locked(m_clockTargetFrames),
            frames_to_ms_locked(m_offeredFrames),
            frames_to_ms_locked(m_submittedFrames),
            frames_to_ms_locked(m_playedFrames),
            frames_to_ms_locked(queued_frames_locked()),
            frames_to_ms_locked(m_writeInProgressFrames),
            frames_to_ms_locked(buffered_frames_locked()),
            frames_to_ms_locked(free_frames_locked()),
            frames_to_ms_locked(m_capacityFrames),
            flags.c_str()
        );
    }

    std::chrono::steady_clock::duration startup_silence_duration() const {
        return std::chrono::duration_cast<std::chrono::steady_clock::duration>(
            std::chrono::milliseconds(m_settings.startupSilenceMs)
        );
    }

    uint64_t startup_delay_frames_locked(
        std::chrono::steady_clock::time_point now
    ) const {
        auto effectiveNow = now;
        if (m_paused.load() &&
            m_pauseStarted != std::chrono::steady_clock::time_point{}) {
            effectiveNow = m_pauseStarted;
        }
        if (!m_clockStarted || m_sampleRate == 0 ||
            effectiveNow >= m_clockAnchor) {
            return 0;
        }
        const auto remaining = std::chrono::duration<double>(
            m_clockAnchor - effectiveNow
        );
        return static_cast<uint64_t>(
            remaining.count() * static_cast<double>(m_sampleRate)
        );
    }

    void finish_playback_clock_if_drained_locked() {
        if (m_drainRequested && buffered_frames_locked() == 0) {
            m_drainRequested = false;
            m_playing.store(false);
        }
    }

    void throw_if_failed_locked() const {
        if (!m_failure.empty()) {
            console::printf("%s: %s", kComponentName, m_failure.c_str());
            throw exception_output_invalidated();
        }
    }

    void set_failure(const std::string& message) {
        {
            std::lock_guard lock(m_mutex);
            if (m_failure.empty()) m_failure = message;
        }
        m_playing.store(false);
        m_cv.notify_all();
    }

    void set_failure_if_current(
        const std::string& message,
        uint64_t generation
    ) {
        bool recorded = false;
        {
            std::lock_guard lock(m_mutex);
            if (!m_shutdown && !m_restart && !m_flushing &&
                generation == m_generation &&
                !m_childStopping.load() && m_failure.empty()) {
                m_failure = message;
                recorded = true;
            }
        }
        if (recorded) {
            m_playing.store(false);
            m_cv.notify_all();
        }
    }

    void set_protocol_state_if_current(
        uint64_t generation,
        bool ready,
        bool audioStarted,
        bool playing
    ) {
        bool accepted = false;
        {
            std::lock_guard lock(m_mutex);
            if (!m_shutdown && !m_restart &&
                generation == m_generation &&
                !m_childStopping.load() &&
                (!m_flushing || playing)) {
                if (ready) m_helperReady.store(true);
                if (audioStarted || playing) {
                    start_playback_clock_locked(
                        std::chrono::steady_clock::now()
                    );
                }
                if (playing) {
                    m_playing.store(true);
                    m_childReachedPlaying.store(true);
                    // Not when the helper was launched: between the spawn and
                    // this line the level has not been applied yet, so a seek
                    // in that window would hand the replacement a raised clamp
                    // over a speaker still sitting wherever it was left.
                    // `WAMBRIDGE PLAYING volume=<step>` is the helper saying it
                    // applied one.
                    m_startupVolumeApplied.store(true);
                }
                accepted = true;
            }
        }
        if (accepted) m_cv.notify_all();
    }

    bool session_matches_locked(
        uint64_t generation,
        unsigned sampleRate,
        unsigned channels
    ) const {
        return generation == m_generation &&
            sampleRate == m_sampleRate &&
            channels == m_channels;
    }

    std::wstring command_line(unsigned sampleRate, unsigned channels) const {
        std::wstring command = quoted(m_settings.helper);
        command += L" --device " + quoted(m_settings.device);
        command += L" --sample-rate " + std::to_wstring(sampleRate);
        command += L" --channels " + std::to_wstring(channels);
        command += L" --sample-format f32le --format " + m_settings.format;
        command += L" --startup-timeout 45";
        command += L" --startup-silence " +
            std::to_wstring(m_settings.startupSilenceMs);
        // Three rules, in the order that makes them agree.
        //
        // With the slider routed, the slider is the answer: the helper mutes
        // for startup and restores this level afterwards, so restoring the INI
        // value would land the speaker somewhere the slider does not point. A
        // physical run started the slider at maximum, the helper restored 3,
        // and the first touch of the slider jumped the speaker to 10.
        const int routed = m_lastVolumeStep.load();
        if (m_settings.hardwareVolume && routed >= 0) {
            // Capped for the first helper of a session only, and not at all
            // once one has reported PLAYING: after that the listener is at the
            // controls and a seek must not turn them down. The helper's own
            // --max-start-volume cannot do this job, because an explicit level
            // wins over that clamp outright - which is how routing the slider
            // silently disabled the safe start it looks like it respects.
            const int level = m_startupVolumeApplied.load() ||
                    m_settings.startVolumeMax <= 0
                ? routed
                : (std::min)(routed, m_settings.startVolumeMax);
            command += L" --volume " + std::to_wstring(level);
        } else if (m_settings.volume.has_value() && !m_startupVolumeApplied.load()) {
            // Otherwise the configured level, but only until some helper of
            // this session has reported PLAYING - that line is the helper
            // saying it applied one. A seek restarts the helper mid-session and
            // passing the configured level again would overwrite whatever the
            // listener has since set from the menu: measured on the M5 on
            // 2026-08-08, volume walked up to 11, one seek, "Speaker volume is
            // 11; starting PCM playback at 3".
            //
            // The flag deliberately does not follow the spawn. A helper
            // replaced before it reached PLAYING may never have applied
            // anything, so its successor starts over rather than inheriting a
            // raised clamp.
            command += L" --volume " + std::to_wstring(*m_settings.volume);
        } else if (m_startupVolumeApplied.load()) {
            // A replacement helper with no level still has to be told
            // something, because it mutes for startup and restores afterwards.
            // Without this it would restore its own default clamp of 10 and a
            // listener sitting above that would still be turned down by a seek.
            command += L" --max-start-volume " +
                std::to_wstring(kMaximumRawVolume);
        }
        return command;
    }

    bool start_child(
        unsigned sampleRate,
        unsigned channels,
        uint64_t generation
    ) {
        stop_child();
        {
            std::lock_guard lock(m_mutex);
            if (m_shutdown || m_restart || !m_failure.empty() ||
                !session_matches_locked(generation, sampleRate, channels)) {
                return false;
            }
        }

        m_childStopping.store(false);
        m_helperReady.store(false);
        m_childReachedPlaying.store(false);
        {
            std::lock_guard lock(m_mutex);
            m_childExited = false;
        }

        SECURITY_ATTRIBUTES security{};
        security.nLength = sizeof(security);
        security.bInheritHandle = TRUE;

        HANDLE stdinRead = nullptr;
        HANDLE stdinWrite = nullptr;
        HANDLE stdoutRead = nullptr;
        HANDLE stdoutWrite = nullptr;
        if (!CreatePipe(&stdinRead, &stdinWrite, &security, 0)) {
            set_failure_if_current(
                "Could not create helper stdin pipe",
                generation
            );
            return false;
        }
        if (!CreatePipe(&stdoutRead, &stdoutWrite, &security, 0)) {
            close_handle(stdinRead);
            close_handle(stdinWrite);
            set_failure_if_current(
                "Could not create helper stdout pipe",
                generation
            );
            return false;
        }
        SetHandleInformation(stdinWrite, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(stdoutRead, HANDLE_FLAG_INHERIT, 0);

        STARTUPINFOEXW startup{};
        startup.StartupInfo.cb = sizeof(startup);
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        startup.StartupInfo.hStdInput = stdinRead;
        startup.StartupInfo.hStdOutput = stdoutWrite;
        startup.StartupInfo.hStdError = stdoutWrite;

        SIZE_T attributeListSize = 0;
        InitializeProcThreadAttributeList(
            nullptr,
            1,
            0,
            &attributeListSize
        );
        if (attributeListSize == 0) {
            close_handle(stdinRead);
            close_handle(stdinWrite);
            close_handle(stdoutRead);
            close_handle(stdoutWrite);
            set_failure_if_current(
                "Could not prepare helper handle inheritance",
                generation
            );
            return false;
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
            close_handle(stdinRead);
            close_handle(stdinWrite);
            close_handle(stdoutRead);
            close_handle(stdoutWrite);
            set_failure_if_current(
                "Could not initialize helper handle inheritance",
                generation
            );
            return false;
        }

        HANDLE inheritedHandles[] = {stdinRead, stdoutWrite};
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
            close_handle(stdinRead);
            close_handle(stdinWrite);
            close_handle(stdoutRead);
            close_handle(stdoutWrite);
            set_failure_if_current(
                "Could not restrict helper handle inheritance",
                generation
            );
            return false;
        }

        PROCESS_INFORMATION process{};
        auto command = command_line(sampleRate, channels);
        std::vector<wchar_t> mutableCommand(command.begin(), command.end());
        mutableCommand.push_back(L'\0');

        const BOOL created = CreateProcessW(
            nullptr,
            mutableCommand.data(),
            nullptr,
            nullptr,
            TRUE,
            CREATE_NO_WINDOW | EXTENDED_STARTUPINFO_PRESENT,
            nullptr,
            nullptr,
            &startup.StartupInfo,
            &process
        );
        DeleteProcThreadAttributeList(startup.lpAttributeList);
        close_handle(stdinRead);
        close_handle(stdoutWrite);
        if (!created) {
            close_handle(stdinWrite);
            close_handle(stdoutRead);
            set_failure_if_current(
                "Could not start wambridge-pcm; configure helper in "
                "%LOCALAPPDATA%\\WAMBridge\\foobar.ini",
                generation
            );
            return false;
        }

        {
            std::lock_guard lock(m_childMutex);
            m_childProcess = process.hProcess;
            m_childThread = process.hThread;
            m_childStdin = stdinWrite;
            m_childStdout = stdoutRead;
        }

        bool stale = false;
        {
            std::lock_guard lock(m_mutex);
            stale = m_shutdown || m_restart || !m_failure.empty() ||
                !session_matches_locked(generation, sampleRate, channels);
        }
        if (stale) {
            m_childStopping.store(true);
            stop_child();
            return false;
        }

        m_protocolThread = std::thread(
            &WamOutput::protocol_loop,
            this,
            stdoutRead,
            generation
        );
        return true;
    }

    void protocol_loop(HANDLE output, uint64_t generation) {
        std::string pending;
        char buffer[512];
        DWORD read = 0;
        while (ReadFile(output, buffer, sizeof(buffer), &read, nullptr) && read > 0) {
            pending.append(buffer, buffer + read);
            size_t newline = 0;
            while ((newline = pending.find('\n')) != std::string::npos) {
                auto line = pending.substr(0, newline);
                pending.erase(0, newline + 1);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                if (!line.empty()) {
                    console::printf("%s: %s", kComponentName, line.c_str());
                }
                if (line == "WAMBRIDGE READY") {
                    set_protocol_state_if_current(
                        generation,
                        true,
                        false,
                        false
                    );
                } else if (line == "WAMBRIDGE AUDIO_STARTED") {
                    set_protocol_state_if_current(
                        generation,
                        false,
                        true,
                        false
                    );
                } else if (line.rfind("WAMBRIDGE PLAYING", 0) == 0) {
                    set_protocol_state_if_current(
                        generation,
                        false,
                        false,
                        true
                    );
                    // Put the slider where the speaker actually ended up. It
                    // usually agrees already, but the first helper of a session
                    // starts capped, and a slider still pointing at last
                    // night's level would jump there on the first pixel of
                    // movement - which is the surprise the cap removes, only
                    // deferred to the next time the listener touches it.
                    const auto marker = line.find("volume=");
                    if (marker != std::string::npos) {
                        wam::note_speaker_step(
                            static_cast<int>(std::strtol(
                                line.c_str() + marker + 7,
                                nullptr,
                                10
                            ))
                        );
                    }
                } else if (line.rfind("WAMBRIDGE CONTROL_PORT ", 0) == 0) {
                    connect_control_channel(line.substr(23), generation);
                } else if (line.rfind("WAMBRIDGE ERROR ", 0) == 0) {
                    set_failure_if_current(line.substr(16), generation);
                }
            }
        }
        bool expected = false;
        {
            std::lock_guard lock(m_mutex);
            expected = m_shutdown || m_restart || m_childStopping.load();
        }
        if (!expected) {
            set_failure_if_current(
                "wambridge-pcm exited unexpectedly",
                generation
            );
        }
        {
            std::lock_guard lock(m_mutex);
            if (generation == m_generation) m_childExited = true;
        }
        m_cv.notify_all();
    }

    void worker_loop() {
        std::vector<float> batch;
        bool pacingSilence = false;
        uint64_t pacedSilenceFrames = 0;
        std::chrono::steady_clock::time_point silenceEpoch{};
        while (true) {
            unsigned sampleRate = 0;
            unsigned channels = 0;
            uint64_t generation = 0;
            bool restart = false;
            {
                std::unique_lock lock(m_mutex);
                m_cv.wait(lock, [this] {
                    return m_shutdown ||
                        (m_failure.empty() && (
                            m_restart || m_flushing ||
                            (m_sampleRate != 0 && m_channels != 0 && (
                                !m_queue.empty() || m_childExited ||
                                (m_paused.load() && m_helperReady.load())
                            ))
                        ));
                });
                if (m_shutdown) break;
                restart = m_restart;
                if (restart) m_childStopping.store(true);
                m_restart = false;
                sampleRate = m_sampleRate;
                channels = m_channels;
                generation = m_generation;
            }

            if (restart) stop_child();
            if (sampleRate == 0 || channels == 0) continue;

            bool flushing = false;
            {
                std::lock_guard lock(m_mutex);
                flushing = m_flushing;
            }
            const auto childState = child_state();
            if (childState == ChildState::exited) {
                set_failure_if_current(
                    "wambridge-pcm exited unexpectedly",
                    generation
                );
                stop_child();
                continue;
            }
            if (flushing && childState == ChildState::none) {
                std::lock_guard lock(m_mutex);
                if (m_flushing && generation == m_generation) {
                    retire_stream_locked();
                }
                continue;
            }
            if (childState == ChildState::none) {
                bool hasAudio = false;
                {
                    std::lock_guard lock(m_mutex);
                    hasAudio = !m_queue.empty();
                }
                if (!hasAudio) continue;
                if (!start_child(sampleRate, channels, generation)) continue;
            }

            bool sendSilence = false;
            bool stopAfterFlush = false;
            size_t batchFrames = 0;
            {
                std::unique_lock lock(m_mutex);
                m_cv.wait_until(
                    lock,
                    m_flushing ? m_flushDeadline
                               : std::chrono::steady_clock::time_point::max(),
                    [this, generation, sampleRate, channels] {
                        return m_shutdown || !m_failure.empty() || m_restart ||
                            !session_matches_locked(
                                generation,
                                sampleRate,
                                channels
                            ) ||
                            (m_helperReady.load() && (
                                m_flushing || m_paused.load() ||
                                !m_queue.empty()
                            ));
                    }
                );
                if (m_shutdown) break;
                if (!m_failure.empty() || m_restart ||
                    !session_matches_locked(generation, sampleRate, channels)) {
                    continue;
                }

                if (m_flushing &&
                    std::chrono::steady_clock::now() >= m_flushDeadline) {
                    retire_stream_locked();
                    stopAfterFlush = true;
                } else {
                    sendSilence = m_flushing || m_paused.load();
                }
                if (stopAfterFlush) {
                    batchFrames = 0;
                } else if (!m_helperReady.load()) {
                    continue;
                } else if (!sendSilence && m_queue.empty()) {
                    continue;
                } else {
                    batchFrames = sendSilence
                        ? kWriteBatchFrames
                        : std::min(kWriteBatchFrames, queued_frames_locked());
                    const size_t values = batchFrames * channels;
                    batch.resize(values);
                    if (sendSilence) {
                        std::fill(batch.begin(), batch.end(), 0.0f);
                    } else {
                        pacingSilence = false;
                        pacedSilenceFrames = 0;
                        const double gain = m_gain.load();
                        for (size_t index = 0; index < values; ++index) {
                            const double scaled =
                                static_cast<double>(m_queue.front()) * gain;
                            batch[index] = static_cast<float>(
                                std::clamp(scaled, -1.0, 1.0)
                            );
                            m_queue.pop_front();
                        }
                        m_writeInProgressFrames = batchFrames;
                    }
                }
            }

            if (stopAfterFlush) {
                stop_child();
                continue;
            }

            HANDLE input = nullptr;
            {
                std::lock_guard lock(m_childMutex);
                input = m_childStdin;
            }
            if (input == nullptr) {
                if (!sendSilence) {
                    std::lock_guard lock(m_mutex);
                    if (generation == m_generation) m_writeInProgressFrames = 0;
                }
                m_cv.notify_all();
                continue;
            }

            const auto* bytes = reinterpret_cast<const std::byte*>(batch.data());
            size_t remaining = batch.size() * sizeof(float);
            bool writeFailed = false;
            while (remaining > 0) {
                DWORD written = 0;
                const DWORD request = static_cast<DWORD>(
                    std::min<size_t>(remaining, static_cast<size_t>(MAXDWORD))
                );
                if (!WriteFile(input, bytes, request, &written, nullptr) || written == 0) {
                    writeFailed = true;
                    break;
                }
                bytes += written;
                remaining -= written;
            }
            if (writeFailed) {
                if (!sendSilence) {
                    std::lock_guard lock(m_mutex);
                    if (generation == m_generation) m_writeInProgressFrames = 0;
                }
                m_cv.notify_all();
                set_failure_if_current(
                    "wambridge-pcm closed its PCM input",
                    generation
                );
                stop_child();
                continue;
            }

            if (sendSilence) {
                const auto now = std::chrono::steady_clock::now();
                if (!pacingSilence) {
                    pacingSilence = true;
                    pacedSilenceFrames = 0;
                    silenceEpoch = now;
                }
                pacedSilenceFrames += batchFrames;
                const auto deadline = silenceEpoch +
                    std::chrono::duration_cast<
                        std::chrono::steady_clock::duration
                    >(std::chrono::duration<double>(
                        static_cast<double>(pacedSilenceFrames) / sampleRate
                    ));
                std::unique_lock lock(m_mutex);
                m_cv.wait_until(
                    lock,
                    deadline,
                    [this, generation] {
                        return m_shutdown || m_restart ||
                            generation != m_generation ||
                            (!m_paused.load() && !m_flushing);
                    }
                );
            }

            if (!sendSilence) {
                std::lock_guard lock(m_mutex);
                if (generation == m_generation) {
                    refresh_playback_clock_locked(
                        std::chrono::steady_clock::now()
                    );
                    m_submittedFrames += batchFrames;
                    m_writeInProgressFrames = 0;
                }
            }
            m_cv.notify_all();
        }
    }

    ChildState child_state() const {
        std::lock_guard lock(m_childMutex);
        if (m_childProcess == nullptr) return ChildState::none;
        return WaitForSingleObject(m_childProcess, 0) == WAIT_TIMEOUT
            ? ChildState::running
            : ChildState::exited;
    }

    void cancel_child() {
        m_childStopping.store(true);
        if (m_worker.joinable()) {
            CancelSynchronousIo(m_worker.native_handle());
        }
        std::lock_guard lock(m_childMutex);
        close_handle(m_childStdin);
    }

    void stop_child() {
        m_childStopping.store(true);
        m_helperReady.store(false);
        // Before the process goes: a socket to a helper that is exiting would
        // accept a level and drop it, and the caller would never learn to fall
        // back to the control process.
        {
            std::lock_guard lock(g_controlMutex);
            close_control_socket_locked();
        }
        const DWORD gracefulWait = m_childReachedPlaying.load()
            ? kActiveShutdownGraceMs
            : kStartupShutdownGraceMs;
        HANDLE process = nullptr;
        {
            std::lock_guard lock(m_childMutex);
            close_handle(m_childStdin);
            process = m_childProcess;
        }
        if (process != nullptr &&
            WaitForSingleObject(process, gracefulWait) == WAIT_TIMEOUT) {
            TerminateProcess(process, 1);
            WaitForSingleObject(process, kActiveShutdownGraceMs);
        }
        if (m_protocolThread.joinable()) m_protocolThread.join();
        m_helperReady.store(false);

        {
            std::lock_guard lock(m_childMutex);
            close_handle(m_childStdout);
            close_handle(m_childThread);
            close_handle(m_childProcess);
        }
        m_playing.store(false);
        m_childReachedPlaying.store(false);

        bool keepStopping = false;
        {
            std::lock_guard lock(m_mutex);
            keepStopping = m_restart || m_shutdown;
        }
        m_childStopping.store(keepStopping);
    }

    const double m_bufferLength;
    const Settings m_settings;

    mutable std::mutex m_mutex;
    std::condition_variable m_cv;
    std::deque<float> m_queue;
    unsigned m_sampleRate = 0;
    unsigned m_channels = 0;
    size_t m_capacityFrames = 0;
    size_t m_writeInProgressFrames = 0;
    uint64_t m_submittedFrames = 0;
    uint64_t m_playedFrames = 0;
    uint64_t m_clockAnchorFrames = 0;
    uint64_t m_clockTargetFrames = 0;
    uint64_t m_offeredFrames = 0;
    unsigned m_counterLines = 0;
    std::chrono::steady_clock::time_point m_lastCounterLog{};
    std::chrono::steady_clock::time_point m_clockAnchor{};
    std::chrono::steady_clock::time_point m_pauseStarted{};
    bool m_clockStarted = false;
    bool m_drainRequested = false;
    bool m_childExited = false;
    uint64_t m_generation = 0;
    bool m_shutdown = false;
    bool m_restart = false;
    bool m_flushing = false;
    std::chrono::steady_clock::time_point m_flushDeadline{};
    std::string m_failure;
    std::atomic<bool> m_paused{false};
    std::atomic<bool> m_playing{false};
    std::atomic<bool> m_helperReady{false};
    std::atomic<bool> m_childStopping{false};
    std::atomic<bool> m_childReachedPlaying{false};
    // Per playback session, not per helper: this object is built when playback
    // starts and torn down when it stops, so a seek cannot clear it.
    std::atomic<bool> m_startupVolumeApplied{false};
    std::atomic<double> m_gain{1.0};
    // -1 until foobar reports the slider position, which it does before the
    // first stream starts. Only meaningful when hardwareVolume is on.
    std::atomic<int> m_lastVolumeStep{-1};
    std::thread m_worker;

    mutable std::mutex m_childMutex;
    HANDLE m_childProcess = nullptr;
    HANDLE m_childThread = nullptr;
    HANDLE m_childStdin = nullptr;
    HANDLE m_childStdout = nullptr;
    std::thread m_protocolThread;
};

output_factory_t<WamOutput> g_outputFactory;

}  // namespace

namespace wam {

bool send_volume_over_helper(int step) {
    const std::string command = "volume " + std::to_string(step) + "\n";
    std::lock_guard lock(g_controlMutex);
    if (g_controlSocket == INVALID_SOCKET) return false;
    const int sent = send(
        g_controlSocket,
        command.c_str(),
        static_cast<int>(command.size()),
        0
    );
    if (sent != static_cast<int>(command.size())) {
        // A half-written command would arrive as a malformed line and be
        // ignored by the helper, so the socket is retired rather than trusted.
        // The caller falls back to spawning the control process.
        close_control_socket_locked();
        return false;
    }
    return true;
}

void note_speaker_step(int step) {
    if (!g_hardwareVolume.load()) return;
    const int ceiling = g_volumeMax.load();
    if (step < 0 || ceiling <= 0) return;
    // Same mapping the slider uses, run backwards, so the round trip is a
    // fixed point: the slider position this produces maps to the same step and
    // the two cannot chase each other.
    main_thread_callback_manager::get()->add_callback(
        new service_impl_t<SliderSync>(
            WamOutput::decibels_for_step(step, ceiling)
        )
    );
}

}  // namespace wam

DECLARE_COMPONENT_VERSION(
    "WAM Bridge Output",
    "0.1.7",
    "Streams foobar2000 PCM to Samsung WAM speakers through wambridge-pcm."
);

VALIDATE_COMPONENT_FILENAME("foo_out_wam.dll");
