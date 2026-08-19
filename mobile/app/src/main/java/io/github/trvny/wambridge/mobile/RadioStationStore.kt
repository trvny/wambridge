package io.github.trvny.wambridge.mobile

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

// TuneIn station ids look like `s15984`. The catalogue also uses `p` for
// podcasts and `t` for individual episodes; neither is a live stream, so
// neither belongs in a station entry.
private val TUNEIN_ID_PATTERN = Regex("^s[0-9]{1,12}$")

/** Return a validated TuneIn station id such as `s15984`. */
internal fun validateTuneInId(value: String): String {
    require(TUNEIN_ID_PATTERN.matches(value)) {
        "TuneIn station id must look like s15984, got '$value'"
    }
    return value
}

internal data class MobileRadioStation(
    val alias: String,
    val urls: List<String>,
    // A TuneIn station id resolves to whatever stream the broadcaster serves
    // today, so it survives an endpoint move that would leave a hardcoded URL
    // dead. It is resolved at play time and never stored as a URL, because the
    // answer changes; `urls` stays as the static safety net for when TuneIn is
    // unreachable or offers only formats the relay refuses.
    val tuneInId: String? = null,
)

internal class RadioStationStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(
        RendererService.PREFS,
        Context.MODE_PRIVATE,
    )

    fun all(): List<MobileRadioStation> = load().sortedBy { it.alias.lowercase() }

    fun upsert(alias: String, urls: List<String>, tuneInId: String? = null): MobileRadioStation {
        val cleanedAlias = alias.trim()
        require(cleanedAlias.isNotEmpty()) { "Station name cannot be empty" }
        val cleanedUrls = validateUrls(urls)
        val station = MobileRadioStation(cleanedAlias, cleanedUrls, cleanTuneInId(tuneInId))
        val stations = load().filterNot { it.alias.equals(cleanedAlias, ignoreCase = true) } + station
        save(stations)
        return station
    }

    fun remove(alias: String) {
        save(load().filterNot { it.alias.equals(alias.trim(), ignoreCase = true) })
    }

    private fun load(): List<MobileRadioStation> {
        val raw = preferences.getString(KEY_STATIONS, null) ?: return emptyList()
        return runCatching {
            val array = JSONArray(raw)
            buildList {
                for (index in 0 until array.length()) {
                    val item = array.getJSONObject(index)
                    val alias = item.getString("alias").trim()
                    val urlsJson = item.getJSONArray("urls")
                    val urls = buildList {
                        for (urlIndex in 0 until urlsJson.length()) {
                            add(urlsJson.getString(urlIndex))
                        }
                    }
                    // Stations written before TuneIn ids existed simply have no
                    // such key, and keep loading unchanged.
                    val tuneInId = cleanTuneInId(item.optString(KEY_TUNEIN_ID))
                    if (alias.isNotEmpty()) {
                        add(MobileRadioStation(alias, validateUrls(urls), tuneInId))
                    }
                }
            }
        }.getOrDefault(emptyList())
    }

    private fun save(stations: List<MobileRadioStation>) {
        val array = JSONArray()
        stations.forEach { station ->
            array.put(JSONObject().apply {
                put("alias", station.alias)
                put("urls", JSONArray(station.urls))
                // A station without an id serialises exactly as it did before.
                station.tuneInId?.let { put(KEY_TUNEIN_ID, it) }
            })
        }
        preferences.edit().putString(KEY_STATIONS, array.toString()).apply()
    }

    private fun cleanTuneInId(value: String?): String? =
        value?.trim()?.takeIf(String::isNotEmpty)?.let(::validateTuneInId)

    private fun validateUrls(values: List<String>): List<String> {
        val result = LinkedHashSet<String>()
        values.map(String::trim).filter(String::isNotEmpty).forEach { value ->
            val uri = URI(value)
            require(uri.scheme.equals("http", ignoreCase = true) || uri.scheme.equals("https", ignoreCase = true)) {
                "Radio URL must use HTTP or HTTPS"
            }
            require(!uri.host.isNullOrBlank()) { "Radio URL needs a host" }
            result += value
        }
        require(result.isNotEmpty()) { "Station needs at least one stream URL" }
        return result.toList()
    }

    companion object {
        private const val KEY_STATIONS = "radio_stations"
        private const val KEY_TUNEIN_ID = "tunein_id"
    }
}
