package io.github.trvny.wambridge.mobile

import android.content.Context
import android.util.Log
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets

/**
 * Resolves a saved TuneIn station id to the streams the broadcaster serves now.
 *
 * This is a plain HTTP GET against the public TuneIn catalogue - no credentials,
 * and the speaker is not involved. Asking for `mp3,aac` matters: without it the
 * answer is `#STATUS: 400` for some stations and an HLS playlist for others.
 * Even with it some stations only offer HLS, which neither the relay nor the M5
 * can take, so an empty result is a normal answer rather than a failure.
 */
internal object TuneInResolver {
    private const val TAG = "WamBridgeTuneIn"
    private const val TUNE_URL = "http://opml.radiotime.com/Tune.ashx"
    private const val DIRECT_FORMATS = "mp3,aac"
    private const val TIMEOUT_MS = 6_000

    // A Tune.ashx answer is a short playlist. Anything larger is not one.
    private const val MAX_RESPONSE_BYTES = 64 * 1024

    /**
     * Return the URLs to try for one station, freshest first.
     *
     * Resolution failing, timing out or answering with nothing usable is not
     * fatal: it costs one entry at the front of a list that still holds
     * everything the station had saved. Blocks on the network, so call it from
     * a worker thread.
     */
    fun candidateUrls(context: Context, station: MobileRadioStation): List<String> {
        val tuneInId = station.tuneInId ?: return station.urls
        val resolved = try {
            resolve(context, tuneInId)
        } catch (error: Exception) {
            Log.w(TAG, "Could not resolve TuneIn id $tuneInId for ${station.alias}", error)
            return station.urls
        }
        if (resolved.isEmpty()) {
            Log.i(
                TAG,
                "TuneIn offered no directly playable stream for ${station.alias} " +
                    "($tuneInId); using the saved URLs",
            )
            return station.urls
        }
        val fresh = resolved.toSet()
        return resolved + station.urls.filterNot { it in fresh }
    }

    /** Ask TuneIn for the current stream URLs of one station id. */
    fun resolve(context: Context, tuneInId: String): List<String> {
        validateTuneInId(tuneInId)
        val body = readText(context, URL("$TUNE_URL?id=$tuneInId&formats=$DIRECT_FORMATS"))
        return parseAnswer(context, body, tuneInId)
    }

    private fun parseAnswer(context: Context, body: String, tuneInId: String): List<String> {
        val urls = LinkedHashSet<String>()
        for (line in body.lineSequence()) {
            val candidate = line.trim()
            if (candidate.isEmpty() || candidate.startsWith("#")) continue
            if (!candidate.startsWith("http://") && !candidate.startsWith("https://")) continue
            if (isHls(candidate)) {
                Log.d(TAG, "Skipping HLS variant for $tuneInId: $candidate")
                continue
            }
            // The HLS rule applies to whatever a PLS file holds as well, not
            // only to the addresses TuneIn answers with directly.
            for (expanded in expandPls(context, candidate)) {
                if (isHls(expanded)) continue
                urls += expanded
            }
        }
        return urls.toList()
    }

    /**
     * Return the stream URLs inside a PLS playlist, or the URL unchanged.
     *
     * TuneIn answers for some stations with a `.pls` file rather than a stream.
     * The relay copies bytes straight through, so handing one over sends the M5
     * a text file instead of audio - measured on Czworka, whose `listen.pls`
     * holds a single `File1=` that plays fine.
     *
     * A playlist that cannot be fetched or holds nothing usable falls back to
     * the original URL, so this can only ever add candidates, never remove one.
     */
    private fun expandPls(context: Context, url: String): List<String> {
        if (!url.substringBefore('?').endsWith(".pls", ignoreCase = true)) return listOf(url)
        val body = try {
            readText(context, URL(url))
        } catch (error: Exception) {
            Log.d(TAG, "Could not read PLS playlist $url: ${error.message}")
            return listOf(url)
        }
        val entries = LinkedHashSet<String>()
        for (line in body.lineSequence()) {
            val entry = line.trim()
            if (!entry.startsWith("file", ignoreCase = true)) continue
            val value = entry.substringAfter('=', "").trim()
            if (value.startsWith("http://") || value.startsWith("https://")) entries += value
        }
        return if (entries.isEmpty()) listOf(url) else entries.toList()
    }

    private fun isHls(url: String): Boolean = url.contains(".m3u8", ignoreCase = true)

    private fun readText(context: Context, url: URL): String {
        var lastError: Exception? = null

        for (connection in WifiLan.openHttpConnections(context.applicationContext, url)) {
            connection.apply {
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                useCaches = false
                requestMethod = "GET"
                instanceFollowRedirects = true
                setRequestProperty("User-Agent", "WAMBridge-Mobile/0.1")
            }
            try {
                if (connection.responseCode != HttpURLConnection.HTTP_OK) {
                    throw IOException("HTTP ${connection.responseCode} from $url")
                }
                return connection.inputStream.use(::readLimited)
            } catch (error: Exception) {
                lastError = error
            } finally {
                connection.disconnect()
            }
        }
        throw lastError ?: IOException("No active Wi-Fi network")
    }

    private fun readLimited(input: InputStream): String {
        val out = ByteArrayOutputStream()
        val buffer = ByteArray(8 * 1024)
        while (out.size() < MAX_RESPONSE_BYTES) {
            val count = input.read(buffer)
            if (count < 0) break
            out.write(buffer, 0, count)
        }
        return out.toByteArray()
            .copyOf(minOf(out.size(), MAX_RESPONSE_BYTES))
            .toString(StandardCharsets.UTF_8)
    }
}
