package io.github.trvny.wambridge.mobile

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews

class WamBridgeWidget : AppWidgetProvider() {
    override fun onUpdate(context: Context, manager: AppWidgetManager, appWidgetIds: IntArray) {
        appWidgetIds.forEach { update(context, manager, it, RendererService.running) }
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        if (intent.action != ACTION_TOGGLE) return

        if (RendererService.running) {
            context.startService(
                Intent(context, RendererService::class.java).apply {
                    action = RendererService.ACTION_STOP
                },
            )
            updateAll(context, active = false)
            refreshAfterTransition(context)
            return
        }

        val preferences = context.getSharedPreferences(RendererService.PREFS, Context.MODE_PRIVATE)
        val target = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        if (!RendererService.isReasonableIpv4(target)) {
            context.startActivity(
                Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            )
            return
        }

        context.startForegroundService(
            Intent(context, RendererService::class.java).apply {
                action = RendererService.ACTION_START
            },
        )
        updateAll(context, active = true)
        refreshAfterTransition(context)
    }

    private fun refreshAfterTransition(context: Context) {
        val pending = goAsync()
        val appContext = context.applicationContext
        Thread({
            try {
                Thread.sleep(TRANSITION_REFRESH_MS)
                updateAll(appContext)
            } finally {
                pending.finish()
            }
        }, "wam-widget-refresh").start()
    }

    companion object {
        private const val ACTION_TOGGLE = "trvny.wambridge.mobile.WIDGET_TOGGLE"
        private const val TRANSITION_REFRESH_MS = 1_500L

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
            val views = RemoteViews(context.packageName, R.layout.widget_wam_bridge)
            views.setImageViewResource(
                R.id.widget_icon,
                if (active) R.drawable.ic_widget_on else R.drawable.ic_widget_off,
            )
            views.setContentDescription(
                R.id.widget_icon,
                if (active) "WAM Bridge on" else "WAM Bridge off",
            )
            val toggle = Intent(context, WamBridgeWidget::class.java).apply {
                action = ACTION_TOGGLE
            }
            views.setOnClickPendingIntent(
                R.id.widget_root,
                PendingIntent.getBroadcast(
                    context,
                    appWidgetId,
                    toggle,
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
                ),
            )
            manager.updateAppWidget(appWidgetId, views)
        }
    }
}
