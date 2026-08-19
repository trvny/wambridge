package io.github.trvny.wambridge.mobile

import android.content.Context
import android.net.Uri
import java.io.BufferedInputStream
import java.io.IOException
import java.net.HttpURLConnection
import java.net.Socket
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean

internal class SamsungWamChannel(
    context: Context,
    private val speakerIp: String,
    private val clientUuid: String,
    private val listener: Listener? = null,
) : AutoCloseable {
    interface Listener {
        fun onPlaybackStarted()
        fun onReportedError(method: String?, code: String)
    }

    private val appContext = context.applicationContext
    private val running = AtomicBoolean(false)
    private val sendLock = Any()
    private var socket: Socket? = null
    private var readerThread: Thread? = null

    fun connect() {
        synchronized(sendLock) {
            if (socket?.isConnected == true && socket?.isClosed == false) return

            val connection = WifiLan.connectSocket(
                appContext,
                speakerIp,
                PORT,
                CONNECT_TIMEOUT_MS,
            ).apply {
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

    fun selectFunction(function: String) {
        send(
            method = "SetFunc",
            arguments = listOf(Argument("function", function, Kind.STR)),
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
        val bytes = ByteArray(8_192)
        val parser = ResponseParser()
        try {
            val input = BufferedInputStream(activeSocket.getInputStream())
            while (running.get() && !activeSocket.isClosed) {
                try {
                    val count = input.read(bytes)
                    if (count < 0) break
                    parser.feed(bytes, count).forEach(::handleResponseBody)
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

    private fun handleResponseBody(body: String) {
        val method = METHOD_REGEX.find(body)?.groupValues?.getOrNull(1)?.trim()
        val errorCode = RESPONSE_ERROR_REGEX.find(body)?.groupValues?.getOrNull(1)?.trim()
            ?: ERROR_ELEMENT_REGEX.find(body)?.groupValues?.getOrNull(1)?.trim()

        if (!errorCode.isNullOrBlank() && errorCode !in SUCCESS_ERROR_CODES) {
            listener?.onReportedError(method, errorCode)
        }
        if (method.equals("StartPlaybackEvent", ignoreCase = true)) {
            listener?.onPlaybackStarted()
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

    private class ResponseParser {
        private var pending = ByteArray(0)

        fun feed(bytes: ByteArray, count: Int): List<String> {
            if (count <= 0) return emptyList()
            if (pending.size + count > MAX_PENDING_BYTES) {
                throw IOException("WAM response buffer exceeded limit")
            }
            pending += bytes.copyOf(count)

            val bodies = mutableListOf<String>()
            while (pending.isNotEmpty()) {
                val statusStart = indexOf(pending, HTTP_PREFIX)
                if (statusStart < 0) {
                    if (pending.size > HTTP_PREFIX.size) {
                        pending = pending.copyOfRange(pending.size - HTTP_PREFIX.size, pending.size)
                    }
                    break
                }
                if (statusStart > 0) pending = pending.copyOfRange(statusStart, pending.size)

                val headerEnd = indexOf(pending, HEADER_END)
                if (headerEnd < 0) break
                val headerText = String(pending, 0, headerEnd, StandardCharsets.ISO_8859_1)
                val status = STATUS_REGEX.find(headerText)?.groupValues?.getOrNull(1)
                    ?: throw IOException("WAM response missing HTTP status")
                val contentLength = CONTENT_LENGTH_REGEX.find(headerText)
                    ?.groupValues
                    ?.getOrNull(1)
                    ?.toIntOrNull()
                    ?: throw IOException("WAM response missing Content-Length")
                if (contentLength !in 0..MAX_BODY_BYTES) {
                    throw IOException("WAM response body too large")
                }

                val bodyStart = headerEnd + HEADER_END.size
                val messageEnd = bodyStart + contentLength
                if (pending.size < messageEnd) break
                if (status == "200" && contentLength > 0) {
                    bodies += String(pending, bodyStart, contentLength, StandardCharsets.UTF_8)
                }
                pending = pending.copyOfRange(messageEnd, pending.size)
            }
            return bodies
        }

        private fun indexOf(haystack: ByteArray, needle: ByteArray): Int {
            if (needle.isEmpty() || haystack.size < needle.size) return -1
            outer@ for (start in 0..haystack.size - needle.size) {
                for (index in needle.indices) {
                    if (haystack[start + index] != needle[index]) continue@outer
                }
                return start
            }
            return -1
        }
    }

    companion object {
        private const val PORT = 55001
        private const val CONNECT_TIMEOUT_MS = 3_000
        private const val READ_TIMEOUT_MS = 1_000
        private const val MAX_PENDING_BYTES = 1024 * 1024
        private const val MAX_BODY_BYTES = 1024 * 1024
        private val HTTP_PREFIX = "HTTP/".toByteArray(StandardCharsets.US_ASCII)
        private val HEADER_END = "\r\n\r\n".toByteArray(StandardCharsets.US_ASCII)
        private val STATUS_REGEX = Regex("^HTTP/1\\.[01]\\s+(\\d{3})\\b", RegexOption.IGNORE_CASE)
        private val CONTENT_LENGTH_REGEX = Regex(
            "^Content-Length\\s*:\\s*(\\d+)\\s*$",
            setOf(RegexOption.IGNORE_CASE, RegexOption.MULTILINE),
        )
        private val METHOD_REGEX = Regex(
            "<method>\\s*([^<]+?)\\s*</method>",
            setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
        )
        private val RESPONSE_ERROR_REGEX = Regex(
            "<response\\b[^>]*\\berrCode\\s*=\\s*['\"]([^'\"]+)['\"]",
            setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
        )
        private val ERROR_ELEMENT_REGEX = Regex(
            "<errCode\\b[^>]*>\\s*([^<]+?)\\s*</errCode>",
            setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
        )
        private val SUCCESS_ERROR_CODES = setOf("0", "00", "000", "0000")

        fun newClientUuid(): String = UUID.randomUUID().toString()

        fun probe(context: Context, speakerIp: String): Boolean {
            val command = Uri.encode("<name>GetSpkName</name>")
            val url = URL("http://$speakerIp:$PORT/UIC?cmd=$command")
            for (connection in WifiLan.openHttpConnections(context.applicationContext, url)) {
                connection.apply {
                    connectTimeout = CONNECT_TIMEOUT_MS
                    readTimeout = CONNECT_TIMEOUT_MS
                    useCaches = false
                    requestMethod = "GET"
                }
                try {
                    if (connection.responseCode != HttpURLConnection.HTTP_OK) continue
                    connection.inputStream.use { input ->
                        if (input.bufferedReader().readText().isNotBlank()) return true
                    }
                } catch (_: IOException) {
                    // Try the next Wi-Fi network if one exists.
                } finally {
                    connection.disconnect()
                }
            }
            return false
        }
    }
}
