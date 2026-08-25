package io.github.trvny.wambridge.mobile

import android.content.Context

internal object SpeakerTarget {
    fun resolve(context: Context, verifySaved: Boolean = true): String? {
        val appContext = context.applicationContext
        val preferences = appContext.getSharedPreferences(RendererService.PREFS, Context.MODE_PRIVATE)
        val saved = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        val savedIsValid = RendererService.isReasonableIpv4(saved)

        if (
            savedIsValid &&
            (RadioService.running || !verifySaved || SamsungWamChannel.probe(appContext, saved))
        ) {
            return saved
        }

        val discovered = WamDiscovery.discover(appContext, allowScan = true).speakers
        val selected = when {
            discovered.size == 1 -> discovered.single()
            savedIsValid -> discovered.firstOrNull { it.ip == saved }
            else -> null
        } ?: return null

        if (selected.ip != saved) {
            preferences.edit().putString(RendererService.KEY_SPEAKER_IP, selected.ip).apply()
        }
        return selected.ip
    }
}
