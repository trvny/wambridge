#include <windows.h>
#include <mmsystem.h>
#include <objidl.h>

#include <foobar2000/SDK/foobar2000.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
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
constexpr const wchar_t* kStreamFormats[] = {L"flac", L"wav", L"mp3"};
constexpr const wchar_t* kDefaultStreamFormat = L"flac";

// Milliseconds of silence FFmpeg prepends to the stream. Straight added delay
// on a path about 6 s long; kept configurable so the hardware can say whether
// it is still load-bearing. Measured at 0 on 2026-08-08: startup still reaches
// WAMBRIDGE PLAYING.
constexpr int kDefaultStartupSilenceMs = 1500;
constexpr int kMaximumStartupSilenceMs = 10000;

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
// The M5's own scale. Used to disable the helper's start-volume clamp when a
// helper is being replaced mid-session rather than starting one.
constexpr int kMaximumRawVolume = 30;

struct Settings {
    std::wstring helper;
    std::wstring device;
    std::wstring format;
    std::optional<int> volume;
    bool diagnostics = false;
    int startupSilenceMs = kDefaultStartupSilenceMs;
};

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
    const bool diagnostics =
        rawDiagnostics == L"1" || rawDiagnostics == L"true" ||
        rawDiagnostics == L"yes" || rawDiagnostics == L"on";

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

    return {
        std::move(helper),
        std::move(device),
        std::move(format),
        volume,
        diagnostics,
        startupSilenceMs,
    };
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
          m_worker(&WamOutput::worker_loop, this) {}

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
            m_capacityFrames = static_cast<size_t>(
                std::ceil((m_bufferLength + 2.0) * static_cast<double>(m_sampleRate))
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
        m_gain.store(std::pow(10.0, decibels / 20.0));
    }

private:
    enum class ChildState {
        none,
        running,
        exited,
    };

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
        // Only until some helper of this session has reported PLAYING, which is
        // the helper saying it applied a level. A seek or a format change
        // restarts the helper mid-session, and passing the configured level
        // again would overwrite whatever the listener has since set from the
        // menu: measured on the M5 on 2026-08-08, volume walked up to 11, one
        // seek, "Speaker volume is 11; starting PCM playback at 3".
        //
        // The flag deliberately does not follow the spawn. A helper replaced
        // before it reached PLAYING may never have applied anything, so its
        // successor has to start over rather than inherit a raised clamp.
        if (m_settings.volume.has_value() && !m_startupVolumeApplied.load()) {
            command += L" --volume " + std::to_wstring(*m_settings.volume);
        } else if (m_startupVolumeApplied.load()) {
            // The helper mutes for startup and restores afterwards, so it has to
            // be told some level; without this it would restore the default
            // clamp of 10 and a listener sitting at 15 would still be turned
            // down by a seek. The safe clamp guards the start of a session, not
            // the level the listener has just chosen during one.
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

DECLARE_COMPONENT_VERSION(
    "WAM Bridge Output",
    "0.1.7",
    "Streams foobar2000 PCM to Samsung WAM speakers through wambridge-pcm."
);

VALIDATE_COMPONENT_FILENAME("foo_out_wam.dll");
