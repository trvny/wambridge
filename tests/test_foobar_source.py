import re
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
        # The fallback must stay guarded by the validation. Asserting only that
        # the assignment exists would match it anywhere in the file.
        guard = source.index("if (!known) {")
        self.assertLess(guard, source.index("format = kDefaultStreamFormat;", guard))
        self.assertIn('constexpr const wchar_t* kDefaultStreamFormat = L"flac";', source)
        self.assertIn('command += L" --sample-format f32le --format " + m_settings.format;', source)
        self.assertNotIn("--format flac --startup-timeout", source)

    def test_unknown_ini_keys_are_reported(self) -> None:
        # An ignored key looks exactly like a working one from the outside. The
        # owner's file carried hardware_volume, which only exists on an unmerged
        # branch, and nothing said so.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("report_unknown_ini_keys(path);", source)
        # A null key name is what asks for the section's key names.
        self.assertIn('L"wambridge",\n        nullptr,', source)
        self.assertIn("ignoring unknown setting(s) in foobar.ini", source)
        # Windows resolves INI keys case-insensitively, so `Device=M5` is
        # applied. Comparing exactly would report an active setting as dead -
        # the same false impression this function exists to remove.
        self.assertIn("CompareStringOrdinal(", source)
        self.assertIn("CSTR_EQUAL", source)
        self.assertNotIn("if (key == candidate) known = true;", source)
        # A rejected value is the same silence in a different place.
        self.assertIn("unknown format %s, falling back to %s", source)
        # "out of range" also covered `startup_silence=fast`, which is not a
        # range problem and reads as though a number was rejected.
        self.assertIn("startup_silence %s is not a number in 0..%u", source)
        self.assertIn("helper %s does not exist, using the bundled one", source)
        self.assertIn("volume %s is not a number in 0..100", source)
        # `#` is an ordinary character to the profile API, so a file copied from
        # an older example arrives carrying keys called `#format`. Reported, not
        # skipped: `#hardware_volume=1` is a setting someone believes is active.
        self.assertIn("if (key.front() == L'#') {", source)
        self.assertIn("Windows \"\n            \"comments start with ';'", source)

    def test_example_ini_comments_use_the_windows_marker(self) -> None:
        # `#startup_silence=0` is not a disabled setting to Windows; it is a key
        # named `#startup_silence`. The example shipped that way.
        example = SOURCE.parent / "foobar.ini.example"
        for number, line in enumerate(
            example.read_text(encoding="utf-8").splitlines(), start=1
        ):
            self.assertFalse(
                line.startswith("#"),
                f"foobar.ini.example:{number} comments with '#', which the "
                "Windows profile API reads as a key name",
            )

    def test_accepted_formats_match_the_helper(self) -> None:
        # The whitelist and the helper's --format choices are edited in two
        # separate languages. A name in only one of them is either a profile
        # nobody can select from the INI or a command line the helper rejects.
        from wambridge.stream import OUTPUT_PROFILES

        source = SOURCE.read_text(encoding="utf-8")
        declaration = re.search(
            r"constexpr const wchar_t\* kStreamFormats\[\] = \{(.*?)\};",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(
            declaration,
            "kStreamFormats is no longer a single-line constexpr array; this test "
            "reads it as text, so update the pattern rather than the whitelist",
        )
        accepted = set(re.findall(r'L"([^"]+)"', declaration.group(1)))

        self.assertEqual(accepted, set(OUTPUT_PROFILES))

    def test_startup_volume_is_applied_once_per_session(self) -> None:
        # Measured on the M5 on 2026-08-08: the listener walked the speaker up to
        # 11 from the menu, seeked once, and the restarted helper logged
        # "Speaker volume is 11; starting PCM playback at 3" - the configured
        # startup level went back over a level a person had just chosen.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn(
            "if (m_settings.volume.has_value() && !m_startupVolumeApplied.load()) {",
            source,
        )
        # The flag must follow the helper reporting PLAYING, not the spawn. A
        # helper replaced in between may never have applied a level, and its
        # successor would then inherit a raised clamp over a speaker sitting
        # wherever it was left.
        playing = source.index("m_childReachedPlaying.store(true);")
        applied = source.index("m_startupVolumeApplied.store(true);")
        self.assertLess(playing, applied)
        self.assertLess(
            applied,
            source.index("accepted = true;", playing),
            "the flag must be set inside the PLAYING branch",
        )
        # Without a level the helper would restore its own default clamp, which
        # still turns a listener above it down.
        self.assertIn('command += L" --max-start-volume " +', source)

    def test_queue_capacity_is_configurable(self) -> None:
        # Capacity is delay on this path: the queue measured 3.79-3.99 s full of
        # a 4.0 s capacity, so everything allowed here is heard that much later.
        # The 2 s that used to be hardcoded was chosen, never measured, and it
        # is the largest single share of the six seconds that reach the ear.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("constexpr int kDefaultBufferExtraMs = 2000;", source)
        self.assertIn('environment_value(L"WAMBRIDGE_BUFFER_EXTRA")', source)
        self.assertIn('ini_value(L"buffer_extra", L"", path)', source)
        self.assertIn("m_settings.bufferExtraMs / 1000.0", source)
        # And it has to join the known-key list, or the component reports its
        # own setting as unknown - the drift that list exists to catch.
        self.assertIn('L"buffer_extra",', source)
        # A typo in a knob meant for walking down during a measurement would
        # otherwise read as "that value changed nothing".
        self.assertIn("buffer_extra %s is not a number in 0..%u", source)
        # The old constant must be gone, or the knob would do nothing.
        self.assertNotIn("(m_bufferLength + 2.0)", source)

    def test_no_conflict_markers_in_tracked_text(self) -> None:
        # A resolution script that only covered the files git named left markers
        # in the plugin guide, and they were committed and reviewed before
        # anyone noticed.
        root = SOURCE.parents[1]
        for path in sorted(root.glob("docs/*.md")) + sorted(
            root.glob("foobar/*")
        ) + [root / "README.md", root / "AGENTS.md"]:
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue  # binary project files
            for marker in ("<<<<<<< ", "=======\n", ">>>>>>> "):
                self.assertNotIn(marker, text, f"{path.name} carries {marker!r}")

    def test_slider_maps_evenly_across_its_travel(self) -> None:
        # Linear in amplitude put four fifths of the slider into silence: at
        # -20 dB the amplitude is 0.1, which against a ceiling of 10 is step 1,
        # and everything below it is step 0. Reported by a listener as "from
        # -20 dB down it is all the same, silence or nearly".
        source = SOURCE.read_text(encoding="utf-8")

        self.assertNotIn("amplitude * static_cast<double>(ceiling)", source)
        self.assertIn("(decibels - kSilenceDecibels) / span", source)
        # Above the floor the slider is asking for something audible.
        self.assertIn("std::max<long>(1, std::min<long>(ceiling, step))", source)

    def test_menu_actions_move_the_slider(self) -> None:
        # Two ways to change one level. A menu press that moved the speaker
        # without moving the slider left them disagreeing until the next drag
        # yanked the speaker back: "volume to safe level went quiet but the
        # slider did not move".
        source = SOURCE.read_text(encoding="utf-8")
        menu = (SOURCE.parent / "wam_menu.cpp").read_text(encoding="utf-8")

        self.assertIn("void note_speaker_step(int step)", source)
        self.assertIn("static double decibels_for_step(", source)
        self.assertIn("wam::note_speaker_step(step);", menu)
        # playback_control is main-thread only and the dispatcher is not it.
        self.assertIn("main_thread_callback_manager::get()->add_callback(", source)
        # Doing nothing when the slider is not routed, or the menu would fight
        # a slider that means something else entirely.
        self.assertIn("if (!g_hardwareVolume.load()) return;", source)

    def test_control_channel_socket_calls_are_linked(self) -> None:
        # Calling into Winsock without ws2_32 links nothing and the failure is
        # nine LNK2001 lines at the very end of a two-minute build.
        source = SOURCE.read_text(encoding="utf-8")
        project = (SOURCE.parent / "foo_out_wam.vcxproj").read_text(encoding="utf-8")

        self.assertIn("#include <winsock2.h>", source)
        # Before windows.h, or the 1.1 declarations collide with these.
        self.assertLess(
            source.index("#include <winsock2.h>"),
            source.index("#include <windows.h>"),
        )
        self.assertIn("ws2_32.lib", project)

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

    def test_startup_silence_is_configurable(self) -> None:
        # The default prepends 1.5 s of silence to a path measured at about 6 s.
        # It carried no comment and had been there since the initial import;
        # 0 was confirmed on hardware on 2026-08-08 and startup still reached
        # WAMBRIDGE PLAYING.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("constexpr int kDefaultStartupSilenceMs = 1500;", source)
        self.assertIn("int startupSilenceMs = kDefaultStartupSilenceMs;", source)
        self.assertIn('environment_value(L"WAMBRIDGE_STARTUP_SILENCE")', source)
        self.assertIn('ini_value(L"startup_silence", L"", path)', source)
        self.assertIn('command += L" --startup-silence " +', source)
        # Out-of-range values fall back rather than reaching the helper CLI,
        # which would reject them and take the whole stream down.
        self.assertIn("parsed <= kMaximumStartupSilenceMs", source)

    def test_clock_holds_back_by_the_configured_silence(self) -> None:
        # The clock must wait exactly as long as the silence FFmpeg prepends,
        # because that silence is what the speaker plays first. A hardcoded
        # 1.5 s left a phantom delay at startup_silence=0 and marked frames
        # played under real audio at larger values.
        source = SOURCE.read_text(encoding="utf-8")

        self.assertNotIn("kStartupLatencySeconds", source)
        self.assertIn("m_clockAnchor = now + startup_silence_duration();", source)
        self.assertIn(
            "std::chrono::milliseconds(m_settings.startupSilenceMs)",
            source,
        )

    def test_the_start_volume_cap_applies_only_to_the_first_helper(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("constexpr int kDefaultStartVolumeMax = 3;", source)
        self.assertIn('L"start_volume_max",', source)
        self.assertIn('environment_value(L"WAMBRIDGE_START_VOLUME_MAX")', source)
        self.assertIn('ini_value(L"start_volume_max", L"", path)', source)
        # Zero is a real answer - "no cap" - so the range starts there rather
        # than at 1 like volume_max, whose zero would mean a silent ceiling.
        self.assertIn("parsed >= 0 &&\n            parsed <= kMaximumRawVolume", source)
        # The cap is lifted once a helper of this session has reported PLAYING,
        # so a seek cannot turn down a level the listener chose mid-session.
        self.assertIn("m_startupVolumeApplied.load() ||", source)
        self.assertIn("(std::min)(routed, m_settings.startVolumeMax)", source)

    def test_the_slider_follows_the_level_the_helper_reports(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        # Without this the capped start leaves the slider pointing somewhere the
        # speaker is not, and the first touch of it jumps - which is the same
        # surprise the cap exists to remove.
        self.assertIn("wam::note_speaker_step(m_reportedStep);", source)
        # Applied on CONTROL_PORT, not on PLAYING. Moving the slider sends the
        # level back out, and at PLAYING there is no socket yet, so it would
        # fall back to launching a process - a second connection to 55001 while
        # audio streams, which AGENTS.md says can starve the stream.
        self.assertIn("m_reportedStep = parsed_volume_step(line);", source)
        self.assertIn(
            "connect_control_channel(line.substr(23), generation);",
            source,
        )
        # Generation-checked, or a PLAYING left in a retired helper's pipe moves
        # the slider on behalf of a helper being killed.
        self.assertIn("generation_is_current(generation)", source)

    def test_an_unreadable_volume_step_is_not_treated_as_zero(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        # strtol answers 0 for a malformed payload, and 0 is a level: it would
        # silence the speaker rather than do nothing.
        self.assertIn("static int parsed_volume_step(", source)
        self.assertIn("if (*start < '0' || *start > '9') return -1;", source)
        self.assertIn("value < 0 || value > kMaximumRawVolume", source)

    def test_the_routed_start_path_with_no_slider_yet_carries_a_clamp(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        # The branch that reaches neither the slider nor a configured volume was
        # the one with nothing watching it: the helper followed whatever the
        # speaker had been left at, held only by its own default of 10.
        self.assertIn(
            "} else if (m_settings.hardwareVolume && "
            "m_settings.startVolumeMax > 0) {",
            source,
        )
        # Gated on routing, because the promise attached to the cap - "the
        # slider governs everything after the start" - is only true when the
        # slider reaches the speaker at all. And gated on the cap being on:
        # passing the speaker maximum for a disabled cap would raise the clamp
        # above the helper's own default of 10, the opposite of disabling it.
        self.assertIn(
            'command += L" --max-start-volume " +\n'
            "                std::to_wstring(m_settings.startVolumeMax);",
            source,
        )
        # The capped level replaces the slider reading, so a seek passes the
        # cap rather than the stale slider position. Without it the quiet start
        # survived only until the first seek.
        self.assertIn("m_lastVolumeStep.store(level);", source)
        # A configured INI volume is a deliberate choice and stays uncapped.
        self.assertIn(
            'command += L" --volume " + std::to_wstring(*m_settings.volume);',
            source,
        )

    def test_the_slider_sync_needs_a_socket_and_a_level_in_range(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        # Without the socket the send falls back to launching a process, which
        # is a second connection to 55001 while audio streams.
        self.assertIn("bool connect_control_channel(", source)
        self.assertIn("if (connected && m_reportedStep >= 0 &&", source)
        # decibels_for_step clamps to the ceiling, so syncing a level above
        # volume_max would write the ceiling back and turn the speaker down.
        self.assertIn("m_reportedStep <= m_settings.volumeMax &&", source)
        # Reset per helper. One that reaches PLAYING and dies before announcing
        # its control channel would otherwise leave its level for the next
        # helper, which the generation check cannot catch - by then the
        # generation is legitimately current.
        reset = source.index("m_childReachedPlaying.store(false);")
        self.assertLess(reset, source.index("m_reportedStep = -1;", reset))
