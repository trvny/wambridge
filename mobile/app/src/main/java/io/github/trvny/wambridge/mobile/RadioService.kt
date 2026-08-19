package io.github.trvny.wambridge.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class RadioService : Service(), RadioProxyServer.Listener, SamsungWamChannel.Listener {
    private val worker = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, WORKER_THREAD_NAME).apply { isDaemon = true }
    }
    private val startPending = AtomicBoolean(false)

    private var proxy: RadioProxyServer? = null
    private var channel: SamsungWamChannel? = null
    private var station: MobileRadioStation? = null
    private var safeVolumeApplied = false
    private var speakerIp = ""

    @Volatile
    private var destroyed = false

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                startPending.set(false)
                execute {
                    stopRadio()
                    lastStatus = "Stopped"
                    stopSelf()
                }
                return START_NOT_STICKY
            }

            ACTION_PLAY -> {
                val alias = intent.getStringExtra(EXTRA_ALIAS).orEmpty().trim()
                promoteToForeground("Starting radio…")
                if (alias.isBlank()) {
                    lastStatus = "Choose a radio station first."
                    stopSelf()
                    return START_NOT_STICKY
                }
                if (startPending.compareAndSet(false, true)) {
                    execute {
                        try {
                            startStation(alias)
                        } finally {
                            startPending.set(false)
                        }
                    }
                }
            }

            else -> stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        destroyed = true
        startPending.set(false)
        try {
            worker.submit { stopRadio() }.get(TEARDOWN_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (_: Exception) {
            // Best effort during process teardown.
        }
        worker.shutdownNow()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startStation(alias: String) {
        if (destroyed) return
        stopRadio(removeForeground = false)
        releaseRenderer()

        val preferences = getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
        speakerIp = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        if (!RendererService.isReasonableIpv4(speakerIp)) {
            fail("Configure the M5 address first.")
            return
        }

        val selected = RadioStationStore(this).all()
            .firstOrNull { it.alias.equals(alias, ignoreCase = true) }
        if (selected == null) {
            fail("Radio station '$alias' is no longer saved.")
            return
        }

        val clientUuid = preferences.getString(KEY_CLIENT_UUID, null)
            ?: SamsungWamChannel.newClientUuid().also {
                preferences.edit().putString(KEY_CLIENT_UUID, it).apply()
            }

        var activeProxy: RadioProxyServer? = null
        var activeChannel: SamsungWamChannel? = null
        try {
            activeProxy = RadioProxyServer(this, speakerIp, selected, this).also { it.start() }
            activeChannel = SamsungWamChannel(this, speakerIp, clientUuid, this).also { it.connect() }

            // Same startup rule as renderer and desktop radio: keep old firmware
            // silent while switching into URL playback. The proxy callback lifts
            // to step 3 only after the M5 has actually requested audio.
            activeChannel.setVolumeRaw(0)
            activeChannel.setMute(true)
            safeVolumeApplied = false
            activeChannel.offerStream(activeProxy.url)

            station = selected
            proxy = activeProxy
            channel = activeChannel
            activeProxy = null
            activeChannel = null
            running = true
            lastStatus = "Starting ${selected.alias}…"
            publish(lastStatus)
        } catch (error: Exception) {
            runCatching { activeChannel?.setVolumeRaw(0) }
            runCatching { activeChannel?.setMute(true) }
            runCatching { activeChannel?.close() }
            runCatching { activeProxy?.close() }
            fail("Could not start ${selected.alias}: ${error.message ?: error.javaClass.simpleName}")
        }
    }

    private fun releaseRenderer() {
        if (!RendererService.running) return
        startService(
            Intent(this, RendererService::class.java).apply {
                action = RendererService.ACTION_STOP
            },
        )
        val deadline = SystemClock.elapsedRealtime() + RENDERER_STOP_TIMEOUT_MS
        while (RendererService.running && SystemClock.elapsedRealtime() < deadline) {
            Thread.sleep(50)
        }
        check(!RendererService.running) { "Renderer did not release the WAM control channel" }
    }

    override fun onStreamOpened(sourceUrl: String) = execute {
        if (destroyed || !running) return@execute
        if (!safeVolumeApplied) {
            val activeChannel = channel ?: return@execute
            activeChannel.setVolumeRaw(SAFE_START_VOLUME)
            activeChannel.setMute(false)
            safeVolumeApplied = true
        }
        val alias = station?.alias ?: "radio"
        lastStatus = "Playing $alias"
        publish(lastStatus)
    }

    override fun onStreamClosed() = execute {
        if (destroyed || !running) return@execute
        val alias = station?.alias ?: "Radio"
        lastStatus = "$alias stream ended"
        stopRadio()
        stopSelf()
    }

    override fun onProxyError(message: String) = execute {
        if (destroyed || !running) return@execute
        lastStatus = "Radio error: $message"
        stopRadio()
        stopSelf()
    }

    override fun onPlaybackStarted() = execute {
        if (destroyed || !running) return@execute
        val alias = station?.alias ?: "radio"
        lastStatus = "Playing $alias · confirmed"
        publish(lastStatus)
    }

    override fun onReportedError(method: String?, code: String) = execute {
        if (destroyed || !running) return@execute
        val suffix = method?.takeIf(String::isNotBlank)?.let { " · $it" }.orEmpty()
        lastStatus = "M5 error $code$suffix"
        publish(lastStatus)
    }

    private fun stopRadio(removeForeground: Boolean = true) {
        if (channel != null) {
            runCatching { channel?.pause() }
        }
        safeVolumeApplied = false
        runCatching { channel?.close() }
        channel = null
        runCatching { proxy?.close() }
        proxy = null
        station = null
        speakerIp = ""
        running = false
        if (removeForeground) stopForeground(STOP_FOREGROUND_REMOVE)
    }

    private fun fail(message: String) {
        lastStatus = message
        publish(message)
        stopRadio()
        stopSelf()
    }

    private fun execute(action: () -> Unit) {
        if (destroyed) return
        try {
            worker.execute {
                if (!destroyed) action()
            }
        } catch (_: RejectedExecutionException) {
            // Service teardown won the race.
        }
    }

    private fun promoteToForeground(message: String) {
        lastStatus = message
        startForeground(NOTIFICATION_ID, buildNotification(message))
    }

    private fun publish(message: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification(message))
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "WAM Bridge radio",
                NotificationManager.IMPORTANCE_LOW,
            ).apply { setShowBadge(false) }
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
    }

    private fun buildNotification(message: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this,
            31,
            Intent(this, RadioStationsActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val stopIntent = PendingIntent.getService(
            this,
            32,
            Intent(this, RadioService::class.java).apply { action = ACTION_STOP },
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
            .setContentTitle("WAM Bridge · Radio")
            .setContentText(message)
            .setContentIntent(openIntent)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop", stopIntent).build())
            .build()
    }

    companion object {
        const val ACTION_PLAY = "trvny.wambridge.mobile.RADIO_PLAY"
        const val ACTION_STOP = "trvny.wambridge.mobile.RADIO_STOP"
        const val EXTRA_ALIAS = "station_alias"

        private const val KEY_CLIENT_UUID = "radio_client_uuid"
        private const val CHANNEL_ID = "wambridge-radio"
        private const val NOTIFICATION_ID = 5102
        private const val SAFE_START_VOLUME = 3
        private const val RENDERER_STOP_TIMEOUT_MS = 2_500L
        private const val TEARDOWN_TIMEOUT_MS = 1_500L
        private const val WORKER_THREAD_NAME = "wam-mobile-radio"

        @Volatile var running = false
            private set
        @Volatile var lastStatus = "Stopped"
            private set
    }
}
