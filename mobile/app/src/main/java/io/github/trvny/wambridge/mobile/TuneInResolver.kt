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
        val url = URL("$TUNE_URL?id=$tuneInId&formats=$DIRECT_FORMATS")
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
                    throw IOException("TuneIn HTTP ${connection.responseCode}")
                }
                return parsePlaylist(connection.inputStream.use(::readLimited), tuneInId)
            } catch (error: Exception) {
                lastError = error
            } finally {
                connection.disconnect()
            }
        }
        throw lastError ?: IOException("No active Wi-Fi network")
    }

    private fun parsePlaylist(body: String, tuneInId: String): List<String> {
        val urls = LinkedHashSet<String>()
        for (line in body.lineSequence()) {
            val candidate = line.trim()
            if (candidate.isEmpty() || candidate.startsWith("#")) continue
            if (!candidate.startsWith("http://") && !candidate.startsWith("https://")) continue
            if (candidate.contains(".m3u8", ignoreCase = true)) {
                Log.d(TAG, "Skipping HLS variant for $tuneInId: $candidate")
                continue
            }
            urls += candidate
        }
        return urls.toList()
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
