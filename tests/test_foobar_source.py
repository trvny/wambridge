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

    def test_pcm_batches_are_paced_in_realtime(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("buffered_frames_locked()", source)
        self.assertIn("m_inflightFrames = batchFrames;", source)
        self.assertIn("reserve_batch_deadline(", source)
        self.assertIn("wait_for_batch_deadline(", source)
        self.assertIn(
            "session_matches_locked(generation, sampleRate, channels)",
            source,
        )
        self.assertIn("bool inputStarved", source)
        self.assertIn("scheduledStart < now", source)
        self.assertIn("m_pacingEpoch += now - scheduledStart;", source)
        self.assertIn("inputStarved,\n                    batchDeadline", source)
        starvation_marker = "inputStarved = m_pacedFrames > 0"
        self.assertEqual(source.count(starvation_marker), 1)
        self.assertIn(
            "inputStarved = m_pacedFrames > 0 && !m_flushing &&",
            source,
        )
        self.assertNotIn("inputStarved = m_playing.load()", source)
        starvation_check = source.index(starvation_marker)
        outer_wait = source.index("m_cv.wait(lock")
        self.assertLess(starvation_check, outer_wait)
        self.assertIn("m_pacingEpoch", source)
        self.assertIn("m_pacedFrames += batchFrames;", source)
        self.assertNotIn("batchStarted + duration", source)
        self.assertIn("pacing accepted=", source)
        self.assertIn("flush requested", source)
        self.assertIn("end of input", source)
        self.assertIn("bufferMilliseconds", source)
        self.assertIn("%u ms buffer", source)
        self.assertNotIn("%.2f s buffer", source)
        self.assertIn("} else if (!line.empty()) {", source)
        self.assertIn('"0.1.9"', source)
