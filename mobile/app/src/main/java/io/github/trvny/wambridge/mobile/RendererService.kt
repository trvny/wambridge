package io.github.trvny.wambridge.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import kotlin.math.roundToInt

class RendererService : Service(), RendererCallbacks {
    private var renderer: UpnpRenderer? = null
    private var wamChannel: SamsungWamChannel? = null
    private var rendererState: RendererState? = null
    private var speakerIp: String = ""
    private var ownsPlayback = false

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action ?: ACTION_START) {
            ACTION_STOP -> {
                stopRenderer()
                stopSelf()
                return START_NOT_STICKY
            }

            ACTION_START -> startRenderer()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        stopRenderer()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startRenderer() {
        val preferences = getSharedPreferences(PREFS, MODE_PRIVATE)
        val target = preferences.getString(KEY_SPEAKER_IP, "").orEmpty().trim()
        if (!isReasonableIpv4(target)) {
            lastStatus = "Set a valid M5 IPv4 address first."
            stopSelf()
            return
        }

        if (renderer != null && speakerIp == target) return
        stopRenderer()

        speakerIp = target
        val clientUuid = preferences.getString(KEY_CLIENT_UUID, null)
            ?: SamsungWamChannel.newClientUuid().also {
                preferences.edit().putString(KEY_CLIENT_UUID, it).apply()
            }
        val rendererUdn = preferences.getString(KEY_RENDERER_UDN, null)
            ?: SamsungWamChannel.newClientUuid().also {
                preferences.edit().putString(KEY_RENDERER_UDN, it).apply()
            }

        try {
            val channel = SamsungWamChannel(speakerIp, clientUuid)
            channel.connect()
            val state = RendererState(rendererUdn)
            val activeRenderer = UpnpRenderer(this, state, this)
            activeRenderer.start()

            wamChannel = channel
            rendererState = state
            renderer = activeRenderer
            ownsPlayback = false
            running = true
            lastStatus = "Ready · ${activeRenderer.localAddress.hostAddress}:${activeRenderer.port} → $speakerIp"
            startForeground(NOTIFICATION_ID, buildNotification(lastStatus))
        } catch (error: Exception) {
            lastStatus = "Could not start adapter: ${error.message ?: error.javaClass.simpleName}"
            stopRenderer()
            stopSelf()
        }
    }

    private fun stopRenderer() {
        rendererState?.transportState = "STOPPED"
        if (ownsPlayback) {
            try {
                wamChannel?.pause()
            } catch (_: Exception) {
                // Best effort: teardown must not keep the foreground service alive.
            }
        }
        ownsPlayback = false
        try {
            renderer?.close()
        } catch (_: Exception) {
            // Best effort.
        }
        try {
            wamChannel?.close()
        } catch (_: Exception) {
            // Best effort.
        }
        renderer = null
        rendererState = null
        wamChannel = null
        running = false
        speakerIp = ""
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    override fun onPlay(rendererStreamUrl: String) {
        val channel = requireNotNull(wamChannel) { "WAM control channel is not running" }
        if (!ownsPlayback) channel.setVolumeRaw(SAFE_START_VOLUME)
        channel.offerStream(rendererStreamUrl)
        ownsPlayback = true
        rendererState?.transportState = "PLAYING"
        publish("Streaming Neutron → M5")
    }

    override fun onPause() {
        if (ownsPlayback) requireNotNull(wamChannel).pause()
        rendererState?.transportState = "PAUSED_PLAYBACK"
        publish("Paused")
    }

    override fun onStop() {
        if (ownsPlayback) requireNotNull(wamChannel).pause()
        ownsPlayback = false
        rendererState?.transportState = "STOPPED"
        publish("Stopped")
    }

    override fun onVolume(percent: Int) {
        val normalized = percent.coerceIn(0, 100)
        val raw = (normalized * 30.0 / 100.0).roundToInt().coerceIn(0, 30)
        requireNotNull(wamChannel).setVolumeRaw(raw)
        rendererState?.volumePercent = normalized
    }

    override fun onMute(muted: Boolean) {
        requireNotNull(wamChannel).setMute(muted)
        rendererState?.muted = muted
    }

    private fun publish(message: String) {
        lastStatus = message
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification(message))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "WAM Bridge",
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = "Mobile UPnP adapter status"
                setShowBadge(false)
            }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(message: String): Notification {
        val openIntent = Intent(this, MainActivity::class.java)
        val openPendingIntent = PendingIntent.getActivity(
            this,
            1,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = Intent(this, RendererService::class.java).apply { action = ACTION_STOP }
        val stopPendingIntent = PendingIntent.getService(
            this,
            2,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setSmallIcon(R.drawable.ic_qs_tile)
            .setContentTitle("WAM Bridge")
            .setContentText(message)
            .setContentIntent(openPendingIntent)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop", stopPendingIntent).build())
            .build()
    }

    companion object {
        const val ACTION_START = "io.github.trvny.wambridge.mobile.START"
        const val ACTION_STOP = "io.github.trvny.wambridge.mobile.STOP"
        const val PREFS = "mobile-adapter"
        const val KEY_SPEAKER_IP = "speaker_ip"
        private const val KEY_CLIENT_UUID = "wam_client_uuid"
        private const val KEY_RENDERER_UDN = "renderer_udn"
        private const val CHANNEL_ID = "wambridge-renderer"
        private const val NOTIFICATION_ID = 5101
        private const val SAFE_START_VOLUME = 3

        @Volatile var running: Boolean = false
            private set
        @Volatile var lastStatus: String = "Stopped"
            private set

        fun isReasonableIpv4(value: String): Boolean {
            val parts = value.split('.')
            return parts.size == 4 && parts.all { part ->
                val number = part.toIntOrNull()
                part.isNotEmpty() && part.length <= 3 && part.all(Char::isDigit) &&
                    number != null && number in 0..255
            }
        }
    }
}
