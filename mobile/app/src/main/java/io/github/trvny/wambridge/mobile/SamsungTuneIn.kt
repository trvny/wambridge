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

internal object SamsungTuneIn {
    data class Preset(
        val contentId: String,
        val title: String,
        val kind: String,
        val description: String? = null,
        val mediaId: String? = null,
        val thumbnail: String? = null,
    ) {
        val presetType: Int
            get() = when (kind.lowercase()) {
                "speaker" -> 1
                "my" -> 0
                else -> error("Unsupported TuneIn preset kind: $kind")
            }

        val presetIndex: Int
            get() = contentId.toIntOrNull()
                ?: error("Invalid TuneIn preset ID: $contentId")
    }

    fun getPresets(context: Context, speakerIp: String): List<Preset> {
        selectTuneIn(context, speakerIp)
        val body = request(
            context,
            speakerIp,
            apiType = "CPM",
            method = "GetPresetList",
            arguments = listOf(
                Argument("startindex", "0", Kind.DEC),
                Argument("listcount", "100", Kind.DEC),
            ),
            timeoutMs = PRESET_TIMEOUT_MS,
        )
        return parsePresets(body)
    }

    fun playSafely(context: Context, speakerIp: String, preset: Preset) {
        // Selecting TuneIn can wake/resume the previous provider state. Silence
        // the speaker first, just like the desktop implementation does.
        request(
            context,
            speakerIp,
            method = "SetVolume",
            arguments = listOf(Argument("volume", "0", Kind.DEC)),
            powerOn = true,
        )
        request(
            context,
            speakerIp,
            method = "SetMute",
            arguments = listOf(Argument("mute", "on", Kind.STR)),
            powerOn = true,
        )

        try {
            selectTuneIn(context, speakerIp)
            request(
                context,
                speakerIp,
                apiType = "CPM",
                method = "SetPlayPreset",
                arguments = listOf(
                    Argument("presettype", preset.presetType.toString(), Kind.DEC),
                    Argument("presetindex", preset.presetIndex.toString(), Kind.DEC),
                ),
                timeoutMs = PLAY_TIMEOUT_MS,
            )
            waitForPlayback(context, speakerIp)

            request(
                context,
                speakerIp,
                method = "SetVolume",
                arguments = listOf(Argument("volume", SAFE_START_VOLUME.toString(), Kind.DEC)),
                powerOn = true,
            )
            request(
                context,
                speakerIp,
                method = "SetMute",
                arguments = listOf(Argument("mute", "off", Kind.STR)),
                powerOn = true,
            )
        } catch (error: Exception) {
            // Deliberately keep volume 0 + mute on after failed TuneIn startup.
            throw error
        }
    }

    private fun selectTuneIn(context: Context, speakerIp: String) {
        request(
            context,
            speakerIp,
            apiType = "CPM",
            method = "SetSelectRadio",
        )
    }

    private fun waitForPlayback(context: Context, speakerIp: String) {
        val deadline = System.currentTimeMillis() + PLAY_TIMEOUT_MS
        var lastBody = ""
        while (System.currentTimeMillis() < deadline) {
            lastBody = request(
                context,
                speakerIp,
                apiType = "CPM",
                method = "GetRadioInfo",
                timeoutMs = RADIO_INFO_TIMEOUT_MS,
            )
            val values = parseNamedValues(lastBody)
            if (values["cpname"].equals("tunein", ignoreCase = true) &&
                values["playstatus"].equals("play", ignoreCase = true)
            ) {
                return
            }
            Thread.sleep(500)
        }
        throw IOException("TuneIn preset did not start: ${lastBody.take(160)}")
    }

    private fun request(
        context: Context,
        speakerIp: String,
        method: String,
        arguments: List<Argument> = emptyList(),
        apiType: String = "UIC",
        powerOn: Boolean = false,
        timeoutMs: Int = COMMAND_TIMEOUT_MS,
    ): String {
        require(RendererService.isReasonableIpv4(speakerIp)) { "Invalid M5 IPv4 address" }
        val command = buildCommand(method, arguments, powerOn)
        val url = URL("http://$speakerIp:$PORT/$apiType?cmd=${Uri.encode(command)}")
        var lastError: Exception? = null

        for (connection in WifiLan.openHttpConnections(context.applicationContext, url)) {
            connection.apply {
                connectTimeout = timeoutMs
                readTimeout = timeoutMs
                useCaches = false
                requestMethod = "GET"
            }
            try {
                if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                    throw IOException("WAM HTTP ${connection.responseCode}")
                }
                val body = connection.inputStream.use(::readLimited)
                responseError(body)?.let { throw IOException("WAM error $it for $method") }
                return body
            } catch (error: Exception) {
                lastError = error
            } finally {
                connection.disconnect()
            }
        }
        throw lastError ?: IOException("No active Wi-Fi network")
    }

    private fun buildCommand(method: String, arguments: List<Argument>, powerOn: Boolean): String =
        buildString {
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
        val error = Regex(
            "<response\\b[^>]*\\berrCode\\s*=\\s*['\"]([^'\"]+)['\"]",
            setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
        ).find(body)?.groupValues?.getOrNull(1)?.trim()
        return error?.takeUnless { it in SUCCESS_CODES }
    }

    private fun parsePresets(body: String): List<Preset> {
        val parser = Xml.newPullParser().apply { setInput(StringReader(body)) }
        val result = mutableListOf<Preset>()
        var current: MutableMap<String, String>? = null
        var textKey: String? = null

        while (parser.eventType != XmlPullParser.END_DOCUMENT) {
            when (parser.eventType) {
                XmlPullParser.START_TAG -> {
                    val tag = localName(parser.name)
                    if (tag == "preset") {
                        current = mutableMapOf()
                        textKey = null
                    } else if (current != null) {
                        val key = (parser.getAttributeValue(null, "name") ?: tag).lowercase()
                        val value = parser.getAttributeValue(null, "val")
                        if (!value.isNullOrBlank() && value != "empty") {
                            current[key] = value
                            textKey = null
                        } else {
                            textKey = key
                        }
                    }
                }

                XmlPullParser.TEXT -> {
                    val key = textKey
                    val value = parser.text?.trim().orEmpty()
                    if (key != null && value.isNotEmpty()) current?.put(key, value)
                }

                XmlPullParser.END_TAG -> {
                    if (localName(parser.name) == "preset") {
                        current?.let { values ->
                            val contentId = values["contentid"]
                            val title = values["title"]
                            val kind = values["kind"]
                            if (!contentId.isNullOrBlank() && !title.isNullOrBlank() && !kind.isNullOrBlank()) {
                                result += Preset(
                                    contentId = contentId,
                                    title = title,
                                    kind = kind,
                                    description = values["description"],
                                    mediaId = values["mediaid"],
                                    thumbnail = values["thumbnail"],
                                )
                            }
                        }
                        current = null
                    }
                    textKey = null
                }
            }
            parser.next()
        }
        return result
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
    private const val COMMAND_TIMEOUT_MS = 5_000
    private const val PRESET_TIMEOUT_MS = 10_000
    private const val RADIO_INFO_TIMEOUT_MS = 5_000
    private const val PLAY_TIMEOUT_MS = 25_000
    private const val MAX_RESPONSE_BYTES = 1024 * 1024
    private const val SAFE_START_VOLUME = 3
    private val SUCCESS_CODES = setOf("0", "00", "000", "0000")
}
