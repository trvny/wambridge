package io.github.trvny.wambridge.mobile

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

internal data class MobileRadioStation(
    val alias: String,
    val urls: List<String>,
)

internal class RadioStationStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(
        RendererService.PREFS,
        Context.MODE_PRIVATE,
    )

    fun all(): List<MobileRadioStation> = load().sortedBy { it.alias.lowercase() }

    fun upsert(alias: String, urls: List<String>): MobileRadioStation {
        val cleanedAlias = alias.trim()
        require(cleanedAlias.isNotEmpty()) { "Station name cannot be empty" }
        val cleanedUrls = validateUrls(urls)
        val station = MobileRadioStation(cleanedAlias, cleanedUrls)
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
                    if (alias.isNotEmpty()) add(MobileRadioStation(alias, validateUrls(urls)))
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
            })
        }
        preferences.edit().putString(KEY_STATIONS, array.toString()).apply()
    }

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
    }
}
