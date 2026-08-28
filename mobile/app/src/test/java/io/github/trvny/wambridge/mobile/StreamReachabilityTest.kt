package io.github.trvny.wambridge.mobile

import java.io.IOException
import java.net.ServerSocket
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * The guard that refuses a stream URL nothing is serving.
 *
 * What it protects against is the one failure in this project that software cannot undo: a
 * `SetUrlPlayback` aimed at an address the speaker cannot pull wedges the control port, and the
 * commands that would recover it are the ones that then go silent. Only a power cycle clears it.
 */
class StreamReachabilityTest {
    @Test
    fun `accepts a port something is listening on`() {
        ServerSocket(0).use { server ->
            assertStreamReachable("http://127.0.0.1:${server.localPort}/stream.wav")
        }
    }

    @Test
    fun `refuses a port with nothing behind it`() {
        val port = ServerSocket(0).use { it.localPort } // closed again before we probe it

        val error = assertThrowsIo {
            assertStreamReachable("http://127.0.0.1:$port/stream.wav")
        }

        assertTrue(error.message!!, error.message!!.contains("nothing is listening"))
        assertTrue(error.message!!, error.message!!.contains("power-cycles"))
    }

    @Test
    fun `refuses a scheme the speaker cannot fetch`() {
        assertThrowsIo { assertStreamReachable("file:///sdcard/stream.wav") }
    }

    @Test
    fun `refuses a url naming no host`() {
        assertThrowsIo { assertStreamReachable("http:///stream.wav") }
    }

    @Test
    fun `uses the scheme default port when the url omits one`() {
        val seen = mutableListOf<Pair<String, Int>>()
        val record: (String, Int, Int) -> Unit = { host, port, _ -> seen += host to port }

        assertStreamReachable("http://speaker.local/stream.wav", connect = record)
        assertStreamReachable("https://speaker.local/stream.wav", connect = record)

        assertEquals(listOf("speaker.local" to 80, "speaker.local" to 443), seen)
    }

    @Test
    fun `keeps the port the url names`() {
        val seen = mutableListOf<Pair<String, Int>>()
        val record: (String, Int, Int) -> Unit = { host, port, _ -> seen += host to port }

        assertStreamReachable("http://192.168.1.9:8200/stream.wav", connect = record)

        assertEquals(listOf("192.168.1.9" to 8200), seen)
    }

    private fun assertThrowsIo(block: () -> Unit): IOException {
        try {
            block()
        } catch (error: IOException) {
            return error
        }
        fail("expected the offer to be refused")
        error("unreachable")
    }
}
