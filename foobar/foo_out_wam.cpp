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
constexpr double kStartupLatencySeconds = 1.5;
constexpr size_t kWriteBatchFrames = 4096;
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

std::wstring bundled_helper_path() {
    HMODULE module = nullptr;
    if (!GetModuleHandleExW(
            GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            reinterpret_cast<LPCWSTR>(&kOutputGuid),
            &module
        )) {
        return L"wambridge-pcm.exe";
    }

    std::vector<wchar_t> buffer(32768);
    const DWORD size = GetModuleFileNameW(
        module,
        buffer.data(),
        static_cast<DWORD>(buffer.size())
    );
    if (size == 0 || size >= buffer.size()) return L"wambridge-pcm.exe";

    std::wstring modulePath(buffer.data(), size);
    const size_t separator = modulePath.find_last_of(L"\\/");
    if (separator == std::wstring::npos) return L"wambridge-pcm.exe";

    const auto bundled = modulePath.substr(0, separator) +
        L"\\wambridge-pcm\\wambridge-pcm.exe";
    return file_exists(bundled) ? bundled : L"wambridge-pcm.exe";
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

struct Settings {
    std::wstring helper;
    std::wstring device;
    std::optional<int> volume;
};

Settings load_settings() {
    const auto path = config_path();
    auto helper = environment_value(L"WAMBRIDGE_PCM");
    if (helper.empty()) {
        const auto configured = ini_value(L"helper", L"", path);
        helper = configured.empty() || !file_exists(configured)
            ? bundled_helper_path()
            : configured;
    }

    auto device = environment_value(L"WAMBRIDGE_DEVICE");
    if (device.empty()) device = ini_value(L"device", L"M5", path);

    std::optional<int> volume;
    auto rawVolume = environment_value(L"WAMBRIDGE_VOLUME");
    if (rawVolume.empty()) rawVolume = ini_value(L"volume", L"", path);
    if (!rawVolume.empty()) {
        wchar_t* end = nullptr;
        const long parsed = std::wcstol(rawVolume.c_str(), &end, 10);
        if (end != rawVolume.c_str() && *end == L'\0' && parsed >= 0 && parsed <= 100) {
            volume = static_cast<int>(parsed);
        }
    }
    return {std::move(helper), std::move(device), volume};
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
        const double queued = static_cast<double>(queued_frames_locked()) / m_sampleRate;
        return queued + (m_playing.load() ? kStartupLatencySeconds : 0.0);
    }

    void process_samples(const audio_chunk& chunk) override {
        (void)process_samples_v2(chunk);
    }

    size_t process_samples_v2(const audio_chunk& chunk) override {
        if (chunk.get_sample_count() == 0 || chunk.get_channels() == 0) return 0;

        std::unique_lock lock(m_mutex);
        throw_if_failed_locked();
        if (m_paused.load()) return 0;

        const unsigned sampleRate = chunk.get_sample_rate();
        const unsigned channels = chunk.get_channels();
        if (sampleRate != m_sampleRate || channels != m_channels) {
            m_queue.clear();
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

        const size_t freeFrames = free_frames_locked();
        const size_t takenFrames = std::min<size_t>(freeFrames, chunk.get_sample_count());
        if (takenFrames == 0) return 0;
        m_flushing = false;

        const audio_sample* input = chunk.get_data();
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
        if (m_paused.load()) return 0;
        if (m_sampleRate == 0 || m_channels == 0) return SIZE_MAX;
        return free_frames_locked();
    }

    bool is_progressing() override {
        std::lock_guard lock(m_mutex);
        return m_playing.load() && !m_paused.load() && !m_flushing;
    }

    void pause(bool state) override {
        m_paused.store(state);
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
        ++m_generation;
        m_helperReady.store(false);
        m_playing.store(false);
        m_childStopping.store(true);
    }

    size_t queued_frames_locked() const {
        return m_channels == 0 ? 0 : m_queue.size() / m_channels;
    }

    size_t free_frames_locked() const {
        const size_t queued = queued_frames_locked();
        return queued >= m_capacityFrames ? 0 : m_capacityFrames - queued;
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
                if (playing) {
                    m_playing.store(true);
                    m_childReachedPlaying.store(true);
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
        command += L" --sample-format f32le --format flac --startup-timeout 45";
        if (m_settings.volume.has_value()) {
            command += L" --volume " + std::to_wstring(*m_settings.volume);
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

        STARTUPINFOW startup{};
        startup.cb = sizeof(startup);
        startup.dwFlags = STARTF_USESTDHANDLES;
        startup.hStdInput = stdinRead;
        startup.hStdOutput = stdoutWrite;
        startup.hStdError = stdoutWrite;

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
            CREATE_NO_WINDOW,
            nullptr,
            nullptr,
            &startup,
            &process
        );
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
                if (line == "WAMBRIDGE READY") {
                    set_protocol_state_if_current(generation, true, false);
                } else if (line.rfind("WAMBRIDGE PLAYING", 0) == 0) {
                    set_protocol_state_if_current(generation, false, true);
                } else if (line.rfind("WAMBRIDGE ERROR ", 0) == 0) {
                    set_failure_if_current(line.substr(16), generation);
                }
            }
        }
        set_failure_if_current(
            "wambridge-pcm exited unexpectedly",
            generation
        );
    }

    void worker_loop() {
        std::vector<float> batch;
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
                                !m_queue.empty() ||
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
            if (childState == ChildState::none &&
                !start_child(sampleRate, channels, generation)) {
                continue;
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
                                m_flushing || m_paused.load() || !m_queue.empty()
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
                } else if (!m_helperReady.load() ||
                    (!sendSilence && m_queue.empty())) {
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
                        const double gain = m_gain.load();
                        for (size_t index = 0; index < values; ++index) {
                            const double scaled =
                                static_cast<double>(m_queue.front()) * gain;
                            batch[index] = static_cast<float>(
                                std::clamp(scaled, -1.0, 1.0)
                            );
                            m_queue.pop_front();
                        }
                    }
                }
            }
            m_cv.notify_all();

            if (stopAfterFlush) {
                stop_child();
                continue;
            }

            HANDLE input = nullptr;
            {
                std::lock_guard lock(m_childMutex);
                input = m_childStdin;
            }
            if (input == nullptr) continue;

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
                set_failure_if_current(
                    "wambridge-pcm closed its PCM input",
                    generation
                );
                stop_child();
                continue;
            }

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
    "0.1.3",
    "Streams foobar2000 PCM to Samsung WAM speakers through wambridge-pcm."
);

VALIDATE_COMPONENT_FILENAME("foo_out_wam.dll");
