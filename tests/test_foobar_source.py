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
