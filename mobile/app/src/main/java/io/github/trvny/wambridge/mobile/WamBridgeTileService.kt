package io.github.trvny.wambridge.mobile

import android.content.Intent
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService

class WamBridgeTileService : TileService() {
    override fun onStartListening() {
        super.onStartListening()
        refreshTile()
    }

    override fun onClick() {
        super.onClick()

        if (RendererService.running) {
            startService(
                Intent(this, RendererService::class.java).apply { action = RendererService.ACTION_STOP },
            )
            qsTile?.state = Tile.STATE_INACTIVE
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) qsTile?.subtitle = "Off"
            qsTile?.updateTile()
            return
        }

        val preferences = getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
        val target = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        if (!RendererService.isReasonableIpv4(target)) {
            qsTile?.state = Tile.STATE_UNAVAILABLE
            qsTile?.updateTile()
            return
        }

        try {
            startForegroundService(
                Intent(this, RendererService::class.java).apply { action = RendererService.ACTION_START },
            )
            qsTile?.state = Tile.STATE_ACTIVE
            if (Build.VERSION_SDK_INT >= Build.VERSION_CODES.Q) qsTile?.subtitle = "Starting¯ "
        } catch (_: IllegalStateException) {
            qsTile?.state = Tile.STATE_INACTIVE
            if (Build.VERSION_SDK_INT >= Build.VERSION_CODES.Q) qsTile?.subtitle = "Start blocked"
        } catch (_: SecurityException) {
            qsTile?.state = Tile.STATE_INACTIVE
            if (Build.VERSION_SDK_INT >= Build.VERSION_CODES.Q) qsTile?.subtitle = "Start blocked"
        }
        qsTile?.updateTile()
    }

    private fun refreshTile() {
        val tile = qsTile ?: return
        val preferences = getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
        val target = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        val configured = RendererService.isReasonableIpv4(target)

        tile.state = when {
            !configured -> Tile.STATE_UNAVAILABLE
            RendererService.running -> Tile.STATE_ACTIVE
            else -> Tile.STATE_INACTIVE
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            tile.subtitle = when {
                !configured -> "Set up M5"
                RendererService.running -> "On"
                else -> "Off"
            }
        }
        tile.updateTile()
    }
}
