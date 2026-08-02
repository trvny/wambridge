from pathlib import Path
from unittest import TestCase


SOURCE = Path(__file__).parents[1] / "foobar" / "foo_out_wam.cpp"


class FoobarSourceTests(TestCase):
    def test_helper_inherits_only_protocol_pipe_handles(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("STARTUPINFOEXW startup{}", source)
        self.assertNotIn("STARTUPINFOW startup{}", source)
        self.assertIn("startup.StartupInfo.cb = sizeof(startup);", source)
        self.assertIn("PROC_THREAD_ATTRIBUTE_HANDLE_LIST", source)
        self.assertIn(
            "HANDLE inheritedHandles[] = {stdinRead, stdoutWrite};",
            source,
        )
        self.assertIn("EXTENDED_STARTUPINFO_PRESENT", source)
        self.assertIn(
            "DeleteProcThreadAttributeList(startup.lpAttributeList);",
            source,
        )

    def test_clock_counters_are_logged_once_per_second(self) -> None:
        # A physical run showed foobar advancing at a median 11x while every
        # clock term stayed unmeasured. These counters are how the runaway
        # gets attributed to a term instead of a guess.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("void log_counters_locked(", source)
        self.assertIn("log_counters_locked(now);", source)
        self.assertIn("kCounterInterval", source)
        self.assertIn("m_counterLines >= kMaxCounterLines", source)
        for field in (
            "target=%ums",
            "offered=%ums",
            "submitted=%ums",
            "played=%ums",
            "queued=%ums",
            "write=%ums",
            "buffered=%ums",
            "free=%ums",
            "capacity=%ums",
        ):
            self.assertIn(field, source)

    def test_void_process_samples_never_drops_the_remainder(self) -> None:
        # process_samples returns void, so a partial write is invisible to the
        # caller and the dropped remainder still counts as played. Measured on
        # a physical M5: foobar advanced 220 s of track in 22 s while the pipe
        # ran at 1.0x and free space stayed near 100 ms.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertNotIn("(void)process_samples_v2(chunk);", source)
        self.assertIn("while (offset < frames)", source)
        self.assertIn("const size_t taken = submit_chunk(chunk, offset);", source)
        self.assertIn("if (!wait_for_room(generation)) return;", source)
        self.assertIn("bool wait_for_room(uint64_t generation)", source)
        # Waiting must end when the stream is torn down, or stop would hang.
        self.assertIn(
            "if (m_shutdown || m_flushing || generation != m_generation) return false;",
            source,
        )
        # The remainder has to be read from where the accepted part ended.
        self.assertIn("chunk.get_data() + offset * channels", source)
        self.assertIn("std::min<size_t>(freeFrames, total - offset)", source)

    def test_offered_counter_survives_a_format_change(self) -> None:
        # The counter is reset with the clock. Counting the offer before the
        # reset lost it: on the 44.1 -> 48 kHz radio switch offered trailed
        # submitted by a whole chunk for the rest of the stream.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("if (offset == 0) m_offeredFrames += total;", source)
        # Counting must sit after the early return, or a retry counts twice.
        accept = source.index("if (offset == 0) m_offeredFrames += total;")
        self.assertLess(source.index("if (takenFrames == 0) return 0;"), accept)

    def test_clock_counters_are_off_unless_asked_for(self) -> None:
        # Diagnostics, not a feature: a normal session must not get 240 lines.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("bool diagnostics = false;", source)
        self.assertIn('environment_value(L"WAMBRIDGE_DIAGNOSTICS")', source)
        self.assertIn('ini_value(L"diagnostics", L"", path)', source)
        self.assertIn("if (!m_settings.diagnostics) return;", source)

    def test_stream_format_is_configurable_but_validated(self) -> None:
        # The value lands on the helper's command line, so an unknown one would
        # be a rejected argument and take the stream down.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn('environment_value(L"WAMBRIDGE_FORMAT")', source)
        self.assertIn('ini_value(L"format", L"", path)', source)
        self.assertIn("if (!known) format = kDefaultStreamFormat;", source)
        self.assertIn('constexpr const wchar_t* kDefaultStreamFormat = L"flac";', source)
        self.assertIn('command += L" --sample-format f32le --format " + m_settings.format;', source)
        self.assertNotIn("--format flac --startup-timeout", source)

    def test_console_format_avoids_length_modifiers(self) -> None:
        # console::printf is pfc's formatter: %lu and %llu print literally.
        # Every foobar source is checked, not just the output adapter: the
        # first fix reached foo_out_wam.cpp only, and wam_menu.cpp kept
        # swallowing the Windows error code in all four of its failure paths.
        for path in sorted(SOURCE.parent.glob("*.cpp")) + sorted(
            SOURCE.parent.glob("*.h")
        ):
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if line.lstrip().startswith("//"):
                    continue  # the rule itself is written down in comments
                for modifier in ("%lu", "%llu"):
                    self.assertNotIn(
                        modifier,
                        line,
                        f"{path.name}:{number} uses {modifier}",
                    )

    def test_pipe_written_pcm_remains_in_reported_latency(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("m_submittedFrames - m_playedFrames", source)
        self.assertIn("m_writeInProgressFrames + submitted", source)
        self.assertIn("buffered_frames_locked() +", source)
        self.assertIn("m_submittedFrames += batchFrames;", source)

    def test_output_capacity_is_released_by_a_realtime_clock(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("elapsed.count() * static_cast<double>(m_sampleRate)", source)
        self.assertIn("m_clockAnchorFrames + elapsedFrames", source)
        self.assertIn(
            "m_playedFrames = std::min(target, m_submittedFrames);",
            source,
        )
        self.assertNotIn("m_clockAnchor = now;", source)
        self.assertIn("startup_delay_frames_locked(now)", source)
        self.assertNotIn("--sample-format f32le --format flac --re", source)

    def test_audio_start_releases_capacity_before_playback_confirmation(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn('line == "WAMBRIDGE AUDIO_STARTED"', source)
        self.assertIn("if (audioStarted || playing)", source)
        self.assertIn("if (playing) {", source)
        self.assertNotIn("if (m_clockStarted) m_playing.store(true);", source)

    def test_pause_preserves_unelapsed_startup_delay(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("m_pauseStarted = now;", source)
        self.assertIn("m_clockAnchor += now - m_pauseStarted;", source)
        self.assertIn("if (m_paused.load()) m_pauseStarted = now;", source)
        self.assertIn("auto effectiveNow = now;", source)
        self.assertIn("effectiveNow = m_pauseStarted;", source)
        self.assertIn("m_clockAnchor - effectiveNow", source)
        self.assertNotIn(
            "m_clockAnchor = now;\n"
            "                m_clockAnchorFrames = m_playedFrames;",
            source,
        )

    def test_force_play_is_a_transient_drain_request(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("m_drainRequested = true;", source)
        self.assertIn("m_drainRequested = false;", source)
        self.assertIn(
            "if (m_clockStarted && m_helperReady.load()) {",
            source,
        )
        self.assertIn("m_playing.store(true);", source)
        self.assertIn(
            "m_drainRequested && buffered_frames_locked() == 0",
            source,
        )
        self.assertNotIn("m_endOfInput", source)
        self.assertNotIn("m_inputClosed", source)
        self.assertNotIn("close_child_input", source)

    def test_unexpected_helper_exit_is_not_treated_as_eof(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn(
            "expected = m_shutdown || m_restart || m_childStopping.load();",
            source,
        )
        self.assertIn(
            'set_failure_if_current(\n'
            '                    "wambridge-pcm exited unexpectedly",',
            source,
        )

    def test_helper_protocol_and_logs_are_visible_in_console(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn(
            'console::printf("%s: %s", kComponentName, line.c_str());',
            source,
        )
        self.assertIn('line == "WAMBRIDGE READY"', source)
        self.assertIn('line == "WAMBRIDGE AUDIO_STARTED"', source)
        self.assertIn('line.rfind("WAMBRIDGE PLAYING", 0)', source)
        self.assertIn('line.rfind("WAMBRIDGE ERROR ", 0)', source)
