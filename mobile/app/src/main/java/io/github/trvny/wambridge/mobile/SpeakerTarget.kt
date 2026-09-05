package io.github.trvny.wambridge.mobile

import android.content.Context

internal object SpeakerTarget {
    private const val KEY_SPEAKER_DEVICE_ID = "speaker_device_id"
    private const val IDENTITY_TIMEOUT_MS = 1_500
    private val resolutionLock = Any()

    fun resolve(
        context: Context,
        verifySaved: Boolean = true,
        persistResult: Boolean = true,
        shouldContinue: () -> Boolean = { true },
    ): String? = withDiscoveryLock {
        resolveLocked(context, verifySaved, persistResult, shouldContinue)
    }

    private fun resolveLocked(
        context: Context,
        verifySaved: Boolean,
        persistResult: Boolean,
        shouldContinue: () -> Boolean,
    ): String? {
        val appContext = context.applicationContext
        val preferences = appContext.getSharedPreferences(RendererService.PREFS, Context.MODE_PRIVATE)
        val savedIp = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        val savedId = preferences.getString(KEY_SPEAKER_DEVICE_ID, "").orEmpty().trim()
        val savedIsValid = RendererService.isReasonableIpv4(savedIp)

        if (!shouldContinue()) return null
        if (savedIsValid && (RadioService.running || !verifySaved)) return savedIp

        if (savedIsValid) {
            val currentId = identify(appContext, savedIp)
            if (!shouldContinue()) return null
            if (currentId != null && (savedId.isBlank() || currentId == savedId)) {
                if (persistResult) remember(appContext, savedIp, currentId)
                return savedIp
            }
            // Preserve compatibility if this install predates stable IDs and the
            // CPM identity read is unavailable while the basic WAM probe works.
            if (savedId.isBlank() && currentId == null &&
                SamsungWamChannel.probe(appContext, savedIp, IDENTITY_TIMEOUT_MS)
            ) return savedIp
        }

        if (!shouldContinue()) return null
        val discovered = WamDiscovery.discover(
            appContext,
            allowScan = true,
            shouldContinue = shouldContinue,
        ).speakers
        if (!shouldContinue()) return null

        val selected = selectCandidate(savedIp, savedId, discovered) { ip ->
            if (!shouldContinue()) null else identify(appContext, ip)
        } ?: return null

        val selectedId = identify(appContext, selected.ip)
        if (!shouldContinue()) return null
        if (persistResult) remember(appContext, selected.ip, selectedId)
        return selected.ip
    }

    /** Keep discovery/probes off the legacy speaker control port one at a time. */
    internal fun <T> withDiscoveryLock(block: () -> T): T =
        synchronized(resolutionLock) { block() }

    /** Persist an automatically resolved address without discarding its stable device id. */
    fun rememberResolvedIp(context: Context, ip: String) {
        context.applicationContext
            .getSharedPreferences(RendererService.PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(RendererService.KEY_SPEAKER_IP, ip)
            .apply()
    }

    fun rememberManualIp(context: Context, ip: String) {
        val preferences = context.applicationContext
            .getSharedPreferences(RendererService.PREFS, Context.MODE_PRIVATE)
        val previous = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        val editor = preferences.edit().putString(RendererService.KEY_SPEAKER_IP, ip)
        if (previous != ip) editor.remove(KEY_SPEAKER_DEVICE_ID)
        editor.apply()
    }

    private fun remember(context: Context, ip: String, deviceId: String?) {
        val editor = context.getSharedPreferences(RendererService.PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(RendererService.KEY_SPEAKER_IP, ip)
        if (!deviceId.isNullOrBlank()) editor.putString(KEY_SPEAKER_DEVICE_ID, deviceId)
        editor.apply()
    }

    private fun identify(context: Context, ip: String): String? = runCatching {
        SamsungWamChannel.readDeviceId(context, ip, IDENTITY_TIMEOUT_MS)
    }.getOrNull()

    internal fun selectCandidate(
        savedIp: String,
        savedId: String,
        speakers: List<WamDiscovery.Speaker>,
        identify: (String) -> String?,
    ): WamDiscovery.Speaker? {
        if (savedId.isNotBlank()) {
            return speakers.firstOrNull { identify(it.ip) == savedId }
        }
        if (speakers.size == 1) return speakers.single()
        return speakers.firstOrNull { it.ip == savedIp }
    }
}
