package io.github.trvny.wambridge.mobile

import android.content.Context
import android.net.Uri
import android.util.Xml
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.StringReader
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets
import org.xmlpull.v1.XmlPullParser

internal object SpeakerRemote {
    enum class PlaybackToggleResult { PAUSED, PLAYING, NO_NATIVE_PLAYBACK }

    fun toggleNativePlayback(context: Context, speakerIp: String): PlaybackToggleResult {
        val playStatus = request(context, speakerIp, method = "GetPlayStatus")
        if (!playStatus["submode"].equals("cp", ignoreCase = true)) {
            return PlaybackToggleResult.NO_NATIVE_PLAYBACK
        }

        val radio = request(context, speakerIp, method = "GetRadioInfo", apiType = "CPM")
        val cpName = radio["cpname"]
        if (cpName.isNullOrBlank() || cpName.equals("unknown", ignoreCase = true)) {
            return PlaybackToggleResult.NO_NATIVE_PLAYBACK
        }

        val status = radio["playstatus"].orEmpty()
        val command = when {
            status.equals("pause", ignoreCase = true) -> "play"
            status.equals("play", ignoreCase = true) -> "pause"
            else -> throw IOException("TuneIn playback state is unknown: ${status.ifBlank { "empty" }}")
        }
        request(
            context,
            speakerIp,
            method = "SetPlaybackControl",
            arguments = listOf(Argument("playbackcontrol", command, Kind.STR)),
            apiType = "CPM",
            powerOn = true,
        )
        return if (command == "pause") PlaybackToggleResult.PAUSED else PlaybackToggleResult.PLAYING
    }

    fun toggleMute(context: Context, speakerIp: String): Boolean {
        val muted = readMute(context, speakerIp)
        val next = !muted
        request(
            context,
            speakerIp,
            method = "SetMute",
            arguments = listOf(Argument("mute", if (next) "on" else "off", Kind.STR)),
            powerOn = true,
        )
        return next
    }

    fun changeVolume(context: Context, speakerIp: String, delta: Int): Int {
        require(delta != 0) { "Volume delta cannot be zero" }
        val current = readVolume(context, speakerIp)
        val next = (current + delta).coerceIn(MIN_VOLUME, MAX_VOLUME)
        if (next != current) {
            request(
                context,
                speakerIp,
                method = "SetVolume",
                arguments = listOf(Argument("volume", next.toString(), Kind.DEC)),
                powerOn = true,
            )
        }
        return next
    }

    private fun readVolume(context: Context, speakerIp: String): Int {
        val values = request(context, speakerIp, method = "GetVolume")
        val raw = values["volume"] ?: values["volumelevel"] ?: values["level"]
            ?: throw IOException("Speaker did not report volume")
        val volume = raw.toIntOrNull() ?: throw IOException("Invalid speaker volume: $raw")
        if (volume !in MIN_VOLUME..MAX_VOLUME) throw IOException("Speaker volume out of range: $volume")
        return volume
    }

    private fun readMute(context: Context, speakerIp: String): Boolean {
        val values = request(context, speakerIp, method = "GetMute")
        return when (values["mute"]?.trim()?.lowercase()) {
            "on", "1", "true" -> true
            "off", "0", "false" -> false
            else -> throw IOException("Speaker did not report mute state")
        }
    }

    private fun request(
        context: Context,
        speakerIp: String,
        method: String,
        arguments: List<Argument> = emptyList(),
        apiType: String = "UIC",
        powerOn: Boolean = false,
    ): Map<String, String> {
        require(RendererService.isReasonableIpv4(speakerIp)) { "Invalid M5 IPv4 address" }
        val command = buildCommand(method, arguments, powerOn)
        val url = URL("http://$speakerIp:$PORT/$apiType?cmd=${Uri.encode(command)}")
        var lastError: Exception? = null

        for (connection in WifiLan.openHttpConnections(context.applicationContext, url)) {
            connection.apply {
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                useCaches = false
                requestMethod = "GET"
            }
            try {
                if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                    throw IOException("WAM HTTP ${connection.responseCode}")
                }
                val body = connection.inputStream.use(::readLimited)
                responseError(body)?.let { throw IOException("WAM error $it for $method") }
                return parseNamedValues(body)
            } catch (error: Exception) {
                lastError = error
            } finally {
                connection.disconnect()
            }
        }
        throw lastError ?: IOException("No active Wi-Fi network")
    }

    private fun buildCommand(
        method: String,
        arguments: List<Argument>,
        powerOn: Boolean,
    ): String = buildString {
        if (powerOn) append("<pwron>on</pwron>")
        append("<name>").append(xml(method)).append("</name>")
        arguments.forEach { argument ->
            append("<p type=\"")
                .append(if (argument.kind == Kind.STR) "str" else "dec")
                .append("\" name=\"")
                .append(xml(argument.name))
                .append("\" val=\"")
                .append(xml(argument.value))
                .append("\"/>")
        }
    }

    private fun xml(value: String): String = value
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
        .replace("'", "&apos;")

    private fun readLimited(input: java.io.InputStream): String {
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(8 * 1024)
        var total = 0
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            total += count
            if (total > MAX_RESPONSE_BYTES) throw IOException("WAM response too large")
            out.write(buffer, 0, count)
        }
        return out.toString(StandardCharsets.UTF_8.name())
    }

    private fun responseError(body: String): String? {
        val result = Regex(
            "<response\\b[^>]*\\bresult\\s*=\\s*['\"]([^'\"]+)['\"]",
            setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
        ).find(body)?.groupValues?.getOrNull(1)?.trim()
        val error = Regex(
            "<response\\b[^>]*\\berrCode\\s*=\\s*['\"]([^'\"]+)['\"]",
            setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
        ).find(body)?.groupValues?.getOrNull(1)?.trim()
            ?: Regex(
                "<errCode\\b[^>]*>\\s*([^<]+?)\\s*</errCode>",
                setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
            ).find(body)?.groupValues?.getOrNull(1)?.trim()

        if (!result.isNullOrBlank() && !result.equals("ok", ignoreCase = true)) {
            return if (error.isNullOrBlank()) result else "$result/$error"
        }
        return error?.takeUnless { it in SUCCESS_CODES }
    }

    private fun parseNamedValues(body: String): Map<String, String> {
        val parser = Xml.newPullParser().apply { setInput(StringReader(body)) }
        val result = mutableMapOf<String, String>()
        var textKey: String? = null
        while (parser.eventType != XmlPullParser.END_DOCUMENT) {
            when (parser.eventType) {
                XmlPullParser.START_TAG -> {
                    val key = (parser.getAttributeValue(null, "name") ?: localName(parser.name)).lowercase()
                    val value = parser.getAttributeValue(null, "val")
                    if (!value.isNullOrBlank() && value != "empty") {
                        result[key] = value
                        textKey = null
                    } else {
                        textKey = key
                    }
                }

                XmlPullParser.TEXT -> {
                    val value = parser.text?.trim().orEmpty()
                    textKey?.let { if (value.isNotEmpty()) result[it] = value }
                }

                XmlPullParser.END_TAG -> textKey = null
            }
            parser.next()
        }
        return result
    }

    private fun localName(name: String): String = name.substringAfterLast(':').lowercase()

    private data class Argument(val name: String, val value: String, val kind: Kind)
    private enum class Kind { STR, DEC }

    private const val PORT = 55001
    private const val TIMEOUT_MS = 5_000
    private const val MAX_RESPONSE_BYTES = 1024 * 1024
    private const val MIN_VOLUME = 0
    private const val MAX_VOLUME = 30
    private val SUCCESS_CODES = setOf("0", "00", "000", "0000")
}
