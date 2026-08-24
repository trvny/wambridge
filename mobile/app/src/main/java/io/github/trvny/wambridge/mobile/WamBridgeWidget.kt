package io.github.trvny.wambridge.mobile

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.widget.RemoteViews
import android.widget.Toast

class WamBridgeWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, appWidgetIds: IntArray) {
        appWidgetIds.forEach { update(context, manager, it, RendererService.running) }
    }

    override fun onAppWidgetOptionsChanged(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetId: Int,
        newOptions: Bundle,
    ) {
        update(context, appWidgetManager, appWidgetId, RendererService.running)
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        when (intent.action) {
            ACTION_TOGGLE -> toggleBridge(context)
            ACTION_PLAY_PAUSE,
            ACTION_MUTE,
            ACTION_VOLUME_DOWN,
            ACTION_VOLUME_UP,
            -> runRemoteAction(context, intent.action.orEmpty())
        }
    }

    private fun toggleBridge(context: Context) {
        if (RendererService.running) {
            context.startService(
                Intent(context, RendererService::class.java).apply {
                    action = RendererService.ACTION_STOP
                },
            )
            updateAll(context, active = false)
            refreshAfterTransition(context, expectedActive = false)
            return
        }

        val appContext = context.applicationContext
        updateAll(appContext, active = true)
        try {
            appContext.startForegroundService(
                Intent(appContext, RendererService::class.java).apply {
                    action = RendererService.ACTION_START
                },
            )
            refreshAfterTransition(appContext, expectedActive = true)
        } catch (error: Exception) {
            updateAll(appContext, active = false)
            showToast(appContext, error.message ?: error.javaClass.simpleName)
        }
    }

    private fun runRemoteAction(context: Context, action: String) {
        val pending = goAsync()
        val appContext = context.applicationContext
        Thread({
            try {
                check(!RendererService.running && !RadioService.running) {
                    "The local adapter currently owns speaker control"
                }
                val target = SpeakerTarget.resolve(appContext, verifySaved = false)
                if (target == null) {
                    showToast(appContext, "No WAM speaker found")
                    openSettings(appContext)
                    return@Thread
                }
                val message = when (action) {
                    ACTION_PLAY_PAUSE -> when (SpeakerRemote.toggleNativePlayback(appContext, target)) {
                        SpeakerRemote.PlaybackToggleResult.PAUSED -> "TuneIn paused"
                        SpeakerRemote.PlaybackToggleResult.PLAYING -> "TuneIn playing"
                        SpeakerRemote.PlaybackToggleResult.NO_NATIVE_PLAYBACK -> {
                            appContext.startActivity(
                                Intent(appContext, TuneInActivity::class.java)
                                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                            )
                            null
                        }
                    }

                    ACTION_MUTE -> if (SpeakerRemote.toggleMute(appContext, target)) "Muted" else "Unmuted"
                    ACTION_VOLUME_DOWN -> "Volume ${SpeakerRemote.changeVolume(appContext, target, -1)}"
                    ACTION_VOLUME_UP -> "Volume ${SpeakerRemote.changeVolume(appContext, target, 1)}"
                    else -> null
                }
                message?.let { showToast(appContext, it) }
            } catch (error: Exception) {
                showToast(appContext, error.message ?: error.javaClass.simpleName)
            } finally {
                updateAll(appContext)
                pending.finish()
            }
        }, "wam-widget-control").start()
    }

    private fun refreshAfterTransition(context: Context, expectedActive: Boolean) {
        val pending = goAsync()
        val appContext = context.applicationContext
        Thread({
            try {
                awaitRendererState(expectedActive)
                updateAll(appContext)
            } finally {
                pending.finish()
            }
        }, "wam-widget-refresh").start()
    }

    private fun awaitRendererState(expectedActive: Boolean) {
        val deadline = SystemClock.elapsedRealtime() + TRANSITION_TIMEOUT_MS
        while (
            RendererService.running != expectedActive &&
            SystemClock.elapsedRealtime() < deadline
        ) {
            Thread.sleep(100)
        }
    }

    private fun openSettings(context: Context) {
        context.startActivity(
            Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
    }

    private fun showToast(context: Context, message: String) {
        Handler(Looper.getMainLooper()).post {
            Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
        }
    }

    companion object {
        private const val ACTION_TOGGLE = "trvny.wambridge.mobile.WIDGET_TOGGLE"
        private const val ACTION_PLAY_PAUSE = "trvny.wambridge.mobile.WIDGET_PLAY_PAUSE"
        private const val ACTION_MUTE = "trvny.wambridge.mobile.WIDGET_MUTE"
        private const val ACTION_VOLUME_DOWN = "trvny.wambridge.mobile.WIDGET_VOLUME_DOWN"
        private const val ACTION_VOLUME_UP = "trvny.wambridge.mobile.WIDGET_VOLUME_UP"
        private const val TRANSITION_TIMEOUT_MS = 20_000L
        private const val EXPANDED_MIN_DP = 100

        fun updateAll(context: Context, active: Boolean = RendererService.running) {
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, WamBridgeWidget::class.java))
            ids.forEach { update(context, manager, it, active) }
        }

        private fun update(
            context: Context,
            manager: AppWidgetManager,
            appWidgetId: Int,
            active: Boolean,
        ) {
            val options = manager.getAppWidgetOptions(appWidgetId)
            val expanded = options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH) >= EXPANDED_MIN_DP &&
                options.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT) >= EXPANDED_MIN_DP
            if (expanded) {
                updateControls(context, manager, appWidgetId)
            } else {
                updateToggle(context, manager, appWidgetId, active)
            }
        }

        private fun updateToggle(
            context: Context,
            manager: AppWidgetManager,
            appWidgetId: Int,
            active: Boolean,
        ) {
            val views = RemoteViews(context.packageName, R.layout.widget_wam_bridge)
            views.setImageViewResource(
                R.id.widget_icon,
                if (active) R.drawable.ic_widget_on else R.drawable.ic_widget_off,
            )
            views.setContentDescription(
                R.id.widget_icon,
                if (active) "WAM Bridge renderer on" else "WAM Bridge renderer off",
            )
            views.setOnClickPendingIntent(
                R.id.widget_root,
                broadcast(context, appWidgetId, ACTION_TOGGLE, 0),
            )
            manager.updateAppWidget(appWidgetId, views)
        }

        private fun updateControls(
            context: Context,
            manager: AppWidgetManager,
            appWidgetId: Int,
        ) {
            val views = RemoteViews(context.packageName, R.layout.widget_wam_bridge_controls)
            views.setOnClickPendingIntent(
                R.id.widget_play_pause,
                broadcast(context, appWidgetId, ACTION_PLAY_PAUSE, 1),
            )
            views.setOnClickPendingIntent(
                R.id.widget_mute,
                broadcast(context, appWidgetId, ACTION_MUTE, 2),
            )
            views.setOnClickPendingIntent(
                R.id.widget_volume_down,
                broadcast(context, appWidgetId, ACTION_VOLUME_DOWN, 3),
            )
            views.setOnClickPendingIntent(
                R.id.widget_volume_up,
                broadcast(context, appWidgetId, ACTION_VOLUME_UP, 4),
            )
            manager.updateAppWidget(appWidgetId, views)
        }

        private fun broadcast(
            context: Context,
            appWidgetId: Int,
            action: String,
            offset: Int,
        ): PendingIntent {
            val intent = Intent(context, WamBridgeWidget::class.java).apply { this.action = action }
            return PendingIntent.getBroadcast(
                context,
                appWidgetId * 10 + offset,
                intent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
            )
        }
    }
}
