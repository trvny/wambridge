package io.github.trvny.wambridge.mobile

import android.content.ComponentName
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
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
            showTile(Tile.STATE_INACTIVE, "Stopping…")
            refreshAfterTransition(expectedActive = false)
            return
        }

        showTile(Tile.STATE_ACTIVE, "Finding M5…")
        val appContext = applicationContext
        Thread({
            val target = SpeakerTarget.resolve(appContext)
            if (target == null) {
                Handler(Looper.getMainLooper()).post {
                    showTile(Tile.STATE_INACTIVE, "M5 not found")
                }
                return@Thread
            }

            try {
                appContext.startForegroundService(
                    Intent(appContext, RendererService::class.java).apply {
                        action = RendererService.ACTION_START
                    },
                )
                Handler(Looper.getMainLooper()).post {
                    showTile(Tile.STATE_ACTIVE, "Starting…")
                }
                refreshAfterTransition(expectedActive = true)
            } catch (_: IllegalStateException) {
                Handler(Looper.getMainLooper()).post {
                    showTile(Tile.STATE_INACTIVE, "Start blocked")
                }
            } catch (_: SecurityException) {
                Handler(Looper.getMainLooper()).post {
                    showTile(Tile.STATE_INACTIVE, "Start blocked")
                }
            }
        }, "wam-tile-start").start()
    }

    private fun refreshAfterTransition(expectedActive: Boolean) {
        val appContext = applicationContext
        Thread({
            val deadline = SystemClock.elapsedRealtime() + TRANSITION_TIMEOUT_MS
            while (
                RendererService.running != expectedActive &&
                SystemClock.elapsedRealtime() < deadline
            ) {
                Thread.sleep(100)
            }
            Handler(Looper.getMainLooper()).post {
                refreshTile()
                TileService.requestListeningState(
                    appContext,
                    ComponentName(appContext, WamBridgeTileService::class.java),
                )
            }
        }, "wam-tile-refresh").start()
    }

    private fun showTile(state: Int, subtitle: String) {
        val tile = qsTile ?: return
        tile.state = state
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) tile.subtitle = subtitle
        tile.updateTile()
    }

    private fun refreshTile() {
        val tile = qsTile ?: return
        tile.state = if (RendererService.running) Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            tile.subtitle = if (RendererService.running) "Renderer on" else "Renderer off"
        }
        tile.updateTile()
    }

    companion object {
        private const val TRANSITION_TIMEOUT_MS = 8_000L
    }
}
