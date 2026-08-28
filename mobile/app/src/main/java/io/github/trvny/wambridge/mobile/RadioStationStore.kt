package io.github.trvny.wambridge.mobile

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.URI

private val TUNEIN_ID_PATTERN = Regex("^s[0-9]{1,12}$")

/** Whether a value has the TuneIn *station* shape, without throwing when it does not. */
internal fun isTuneInStationId(value: String): Boolean = TUNEIN_ID_PATTERN.matches(value)

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
    // TuneIn is resolved at play time; saved URLs remain the ordered fallback.
    val tuneInId: String? = null,
)

internal fun mergeRadioStations(
    saved: List<MobileRadioStation>,
    bundled: List<MobileRadioStation>,
    hiddenBundledAliases: Set<String> = emptySet(),
): List<MobileRadioStation> {
    val savedByAlias = saved.associateBy { it.alias.lowercase() }
    val hidden = hiddenBundledAliases.mapTo(mutableSetOf()) { it.lowercase() }
    val merged = bundled.mapNotNull { station ->
        val key = station.alias.lowercase()
        savedByAlias[key] ?: station.takeUnless { key in hidden }
    }.toMutableList()
    val bundledAliases = bundled.mapTo(mutableSetOf()) { it.alias.lowercase() }
    merged += saved.filterNot { it.alias.lowercase() in bundledAliases }
    return merged
}

/**
 * Pick the station a play request names.
 *
 * A station browsed out of the speaker's own TuneIn catalogue is never saved, so
 * it arrives as a title plus a TuneIn id and is played straight from that: the
 * resolver turns the id into stream URLs at play time, which is the same thing it
 * does for a saved station. Anything else is a saved alias.
 */
internal fun radioStationToPlay(
    alias: String,
    tuneInId: String?,
    saved: List<MobileRadioStation>,
): MobileRadioStation? {
    val name = alias.trim()
    val id = tuneInId?.trim()?.takeUnless { it.isEmpty() }
    if (id != null) {
        return MobileRadioStation(
            alias = name.ifEmpty { id },
            urls = emptyList(),
            tuneInId = id,
        )
    }
    if (name.isEmpty()) return null
    return saved.firstOrNull { it.alias.equals(name, ignoreCase = true) }
}

internal class RadioStationStore(context: Context) {
    private val appContext = context.applicationContext
    private val preferences = appContext.getSharedPreferences(
        RendererService.PREFS,
        Context.MODE_PRIVATE,
    )
    private val bundledStations: List<MobileRadioStation> by lazy(::loadBundledFavorites)

    fun all(): List<MobileRadioStation> = mergeRadioStations(
        loadSaved(),
        bundledStations,
        hiddenBundledAliases(),
    ).sortedBy { it.alias.lowercase() }

    fun upsert(alias: String, urls: List<String>, tuneInId: String? = null): MobileRadioStation {
        val cleanedAlias = alias.trim()
        require(cleanedAlias.isNotEmpty()) { "Station name cannot be empty" }
        val station = MobileRadioStation(cleanedAlias, validateUrls(urls), cleanTuneInId(tuneInId))
        val stations = loadSaved().filterNot {
            it.alias.equals(cleanedAlias, ignoreCase = true)
        } + station
        saveSaved(stations)
        unhideBundled(cleanedAlias)
        return station
    }

    fun remove(alias: String) {
        val cleanedAlias = alias.trim()
        saveSaved(loadSaved().filterNot { it.alias.equals(cleanedAlias, ignoreCase = true) })
        if (bundledStations.any { it.alias.equals(cleanedAlias, ignoreCase = true) }) {
            val hidden = hiddenBundledAliases().toMutableSet()
            hidden += cleanedAlias.lowercase()
            preferences.edit().putStringSet(KEY_HIDDEN_BUNDLED, hidden).apply()
        }
    }

    private fun unhideBundled(alias: String) {
        val hidden = hiddenBundledAliases().toMutableSet()
        if (hidden.remove(alias.lowercase())) {
            preferences.edit().putStringSet(KEY_HIDDEN_BUNDLED, hidden).apply()
        }
    }

    private fun hiddenBundledAliases(): Set<String> =
        preferences.getStringSet(KEY_HIDDEN_BUNDLED, emptySet()).orEmpty().toSet()

    private fun loadSaved(): List<MobileRadioStation> {
        val raw = preferences.getString(KEY_STATIONS, null) ?: return emptyList()
        return runCatching {
            val array = JSONArray(raw)
            buildList {
                for (index in 0 until array.length()) {
                    val item = array.getJSONObject(index)
                    val alias = item.getString("alias").trim()
                    val urls = jsonStrings(item.getJSONArray("urls"))
                    val tuneInId = cleanTuneInId(item.optString(KEY_TUNEIN_ID))
                    if (alias.isNotEmpty()) {
                        add(MobileRadioStation(alias, validateUrls(urls), tuneInId))
                    }
                }
            }
        }.getOrDefault(emptyList())
    }

    private fun loadBundledFavorites(): List<MobileRadioStation> = runCatching {
        val text = appContext.assets.open(BUNDLED_ASSET)
            .bufferedReader(Charsets.UTF_8)
            .use { it.readText() }
        val root = JSONObject(text)
        val byAlias = buildMap {
            val stations = root.getJSONArray("stations")
            for (index in 0 until stations.length()) {
                bundledStation(stations.getJSONObject(index))?.let { station ->
                    put(station.alias, station)
                }
            }
        }
        jsonStrings(root.getJSONObject("packs").getJSONArray(DEFAULT_PACK)).mapNotNull(byAlias::get)
    }.getOrDefault(emptyList())

    private fun bundledStation(item: JSONObject): MobileRadioStation? {
        if (!item.optBoolean("mobile_supported", true)) return null
        val alias = item.getString("alias").trim()
        val urls = buildList {
            add(item.getString("url"))
            item.optJSONArray("fallback_urls")?.let { addAll(jsonStrings(it)) }
        }
        return MobileRadioStation(
            alias = alias,
            urls = validateUrls(urls),
            tuneInId = cleanTuneInId(item.optString(KEY_TUNEIN_ID)),
        )
    }

    private fun jsonStrings(array: JSONArray): List<String> = buildList {
        for (index in 0 until array.length()) add(array.getString(index))
    }

    private fun saveSaved(stations: List<MobileRadioStation>) {
        val array = JSONArray()
        stations.forEach { station ->
            array.put(JSONObject().apply {
                put("alias", station.alias)
                put("urls", JSONArray(station.urls))
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
            require(
                uri.scheme.equals("http", ignoreCase = true) ||
                    uri.scheme.equals("https", ignoreCase = true),
            ) { "Radio URL must use HTTP or HTTPS" }
            require(!uri.host.isNullOrBlank()) { "Radio URL needs a host" }
            result += value
        }
        require(result.isNotEmpty()) { "Station needs at least one stream URL" }
        return result.toList()
    }

    companion object {
        private const val KEY_STATIONS = "radio_stations"
        private const val KEY_TUNEIN_ID = "tunein_id"
        private const val KEY_HIDDEN_BUNDLED = "radio_hidden_bundled"
        private const val BUNDLED_ASSET = "station_packs.json"
        private const val DEFAULT_PACK = "favorites"
    }
}
