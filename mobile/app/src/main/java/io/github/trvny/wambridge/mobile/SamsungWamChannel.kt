package io.github.trvny.wambridge.mobile

import android.net.Uri
import java.io.BufferedInputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

internal class SamsungWamChannel(
    private val speakerIp: String,
    private val clientUuid: String,
) : AutoCloseable {
    private val running = AtomicBoolean(false)
    private val sendLock = Any()
    private var socket: Socket? = null
    private var readerThread: Thread? = null

    fun connect() {
        synchronized(sendLock) {
            if (socket?.isConnected == true && socket?.isClosed == false) return

            val connection = Socket().apply {
                connect(InetSocketAddress(speakerIp, PORT), CONNECT_TIMEOUT_MS)
                keepAlive = true
                soTimeout = READ_TIMEOUT_MS
            }
            socket = connection
            running.set(true)
            readerThread = Thread({ drainResponses(connection) }, "wam-mobile-control-reader").apply {
                isDaemon = true
                start()
            }
            send("GetFunc")
        }
    }

    fun offerStream(url: String) {
        send(
            method = "SetUrlPlayback",
            arguments = listOf(
                Argument("url", url, Kind.CDATA),
                Argument("buffersize", "0", Kind.DEC),
                Argument("seektime", "0", Kind.DEC),
                Argument("resume", "0", Kind.DEC),
            ),
        )
    }

    fun pause() {
        send(
            method = "SetPlaybackControl",
            arguments = listOf(Argument("playbackcontrol", "pause", Kind.STR)),
            powerOn = true,
        )
    }

    fun setVolumeRaw(step: Int) {
        require(step in 0..30) { "M5 volume step must be 0..30" }
        send(
            method = "SetVolume",
            arguments = listOf(Argument("volume", step.toString(), Kind.DEC)),
            powerOn = true,
        )
    }

    fun setMute(muted: Boolean) {
        send(
            method = "SetMute",
            arguments = listOf(Argument("mute", if (muted) "on" else "off", Kind.STR)),
            powerOn = true,
        )
    }

    private fun send(
        method: String,
        arguments: List<Argument> = emptyList(),
        apiType: String = "UIC",
        powerOn: Boolean = false,
    ) {
        synchronized(sendLock) {
            if (socket?.isConnected != true || socket?.isClosed != false) connect()
            val activeSocket = requireNotNull(socket)
            val command = buildCommand(method, arguments, powerOn)
            val target = "/$apiType?cmd=${Uri.encode(command)}"
            val request = buildString {
                append("GET ").append(target).append(" HTTP/1.1\r\n")
                append("Host: ").append(speakerIp).append(':').append(PORT).append("\r\n")
                append("mobileUUID: ").append(clientUuid).append("\r\n")
                append("mobileName: Wireless Audio\r\n")
                append("mobileVersion: 1.0\r\n")
                append("Connection: keep-alive\r\n\r\n")
            }
            try {
                activeSocket.getOutputStream().apply {
                    write(request.toByteArray(StandardCharsets.UTF_8))
                    flush()
                }
            } catch (error: IOException) {
                closeSocket()
                throw error
            }
        }
    }

    private fun drainResponses(activeSocket: Socket) {
        val buffer = ByteArray(8192)
        try {
            val input = BufferedInputStream(activeSocket.getInputStream())
            while (running.get() && !activeSocket.isClosed) {
                try {
                    if (input.read(buffer) < 0) break
                } catch (_: java.net.SocketTimeoutException) {
                    continue
                }
            }
        } catch (_: IOException) {
            // The service reports command failures on the next send/reconnect attempt.
        } finally {
            if (socket === activeSocket) closeSocket()
        }
    }

    override fun close() {
        running.set(false)
        closeSocket()
        readerThread?.interrupt()
        readerThread = null
    }

    private fun closeSocket() {
        synchronized(sendLock) {
            try {
                socket?.close()
            } catch (_: IOException) {
                // Best effort during service teardown.
            }
            socket = null
        }
    }

    private fun buildCommand(
        method: String,
        arguments: List<Argument>,
        powerOn: Boolean,
    ): String = buildString {
        if (powerOn) append("<pwron>on</pwron>")
        append("<name>").append(xmlText(method)).append("</name>")
        for (argument in arguments) {
            when (argument.kind) {
                Kind.CDATA -> {
                    append("<p type=\"cdata\" name=\"")
                        .append(xmlAttribute(argument.name))
                        .append("\" val=\"empty\"><![CDATA[")
                        .append(argument.value.replace("]]>", "]]]]><![CDATA[>"))
                        .append("]]></p>")
                }

                Kind.STR, Kind.DEC -> {
                    append("<p type=\"")
                        .append(if (argument.kind == Kind.STR) "str" else "dec")
                        .append("\" name=\"")
                        .append(xmlAttribute(argument.name))
                        .append("\" val=\"")
                        .append(xmlAttribute(argument.value))
                        .append("\"/>")
                }
            }
        }
    }

    private fun xmlText(value: String): String = value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")

    private fun xmlAttribute(value: String): String = xmlText(value)
        .replace("\"", "&quot;")
        .replace("'", "&apos;")

    private data class Argument(val name: String, val value: String, val kind: Kind)
    private enum class Kind { STR, DEC, CDATA }

    companion object {
        private const val PORT = 55001
        private const val CONNECT_TIMEOUT_MS = 3000
        private const val READ_TIMEOUT_MS = 1000

        fun newClientUuid(): String = UUID.randomUUID().toString()

        fun probe(speakerIp: String): Boolean {
            val command = Uri.encode("<name>GetSpkName</name>")
            val connection = (URL("http://$speakerIp:$PORT/UIC?cmd=$command").openConnection() as HttpURLConnection).apply {
                connectTimeout = CONNECT_TIMEOUT_MS
                readTimeout = CONNECT_TIMEOUT_MS
                useCaches = false
                requestMethod = "GET"
            }
            return try {
                connection.inputStream.use { input ->
                    val body = input.bufferedReader().readText()
                    connection.responseCode == HttpURLConnection.HTTP_OK && body.isNotBlank()
                }
            } catch (_: IOException) {
                false
            } finally {
                connection.disconnect()
            }
        }
    }
}
