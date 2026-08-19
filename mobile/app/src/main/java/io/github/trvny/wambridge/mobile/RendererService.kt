package io.github.trvny.wambridge.mobile

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledFuture
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.roundToInt

class RendererService : Service(), RendererCallbacks, SamsungWamChannel.Listener {
    private var renderer: UpnpRenderer? = null
    private var wamChannel: SamsungWamChannel? = null
    private var rendererState: RendererState? = null
    private var speakerIp: String = ""
    private var clientUuid: String = ""
    private var ownsPlayback = false
    private var safeVolumeApplied = false
    private val channelLock = Any()
    private val idleLock = Any()
    private var idleRelease: ScheduledFuture<*>? = null
    private val startPending = AtomicBoolean(false)
    private val worker = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, WORKER_THREAD_NAME).apply { isDaemon = true }
    }
    private val idleScheduler = Executors.newSingleThreadScheduledExecutor { runnable ->
        Thread(runnable, "wam-mobile-idle-release").apply { isDaemon = true }
    }

    @Volatile
    private var destroyed = false

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action ?: ACTION_START) {
            ACTION_STOP -> {
                startPending.set(false)
                worker.execute {
                    stopRenderer()
                    stopSelf()
                }
                return START_NOT_STICKY
            }

            ACTION_START -> {
                promoteToForeground("Starting...")
                if (running) {
                    publish(lastStatus)
                } else if (startPending.compareAndSet(false, true)) {
                    worker.execute {
                        try {
                            startRenderer()
                        } finally {
                            startPending.set(false)
                        }
                    }
                }
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        destroyed = true
        startPending.set(false)
        cancelIdleRelease()

        try {
            worker.submit { stopRenderer() }.get(DESTROY_RELEASE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (_: Exception) {
            // Best effort. The worker is stopped below even if teardown times out.
        }

        idleScheduler.shutdownNow()
        worker.shutdownNow()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun startRenderer() {
        if (destroyed) return

        val preferences = getSharedPreferences(PREFS, MODE_PRIVATE)
        val target = preferences.getString(KEY_SPEAKER_IP, "").orEmpty().trim()
        if (!isReasonableIpv4(target)) {
            lastStatus = "Set a valid M5 IPv4 address first."
            stopRenderer()
            stopSelf()
            return
        }

        if (renderer != null && speakerIp == target) {
            publish(lastStatus)
            return
        }
        stopRenderer(removeForeground = false)

        speakerIp = target
        clientUuid = preferences.getString(KEY_CLIENT_UUID, null)
            ?: SamsungWamChannel.newClientUuid().also {
                preferences.edit().putString(KEY_CLIENT_UUID, it).apply()
            }
        val rendererUdn = preferences.getString(KEY_RENDERER_UDN, null)
            ?: SamsungWamChannel.newClientUuid().also {
                preferences.edit().putString(KEY_RENDERER_UDN, it).apply()
            }

        var activeRenderer: UpnpRenderer? = null
        try {
            val state = RendererState(rendererUdn)
            activeRenderer = UpnpRenderer(this, state, this, target)
            activeRenderer.start()
            if (destroyed || Thread.currentThread().isInterrupted) return

            rendererState = state
            renderer = activeRenderer
            activeRenderer = null

            ownsPlayback = false
            safeVolumeApplied = false
            running = true
            lastStatus = "Ready · ${renderer!!.localAddress.hostAddress}:${renderer!!.port} → $speakerIp · speaker released"
            publish(lastStatus)
        } catch (error: Exception) {
            lastStatus = "Could not start adapter: ${error.message ?: error.javaClass.simpleName}"
            stopRenderer()
            stopSelf()
        } finally {
            try {
                activeRenderer?.close()
            } catch (_: Exception) {
                // Best effort while abandoning a partially started renderer.
            }
        }
    }

    private fun ensureChannel(): SamsungWamChannel = synchronized(channelLock) {
        wamChannel?.let { return it }
        check(speakerIp.isNotBlank()) { "Speaker is not configured" }
        check(clientUuid.isNotBlank()) { "Client UUID is not configured" }
        SamsungWamChannel(applicationContext, speakerIp, clientUuid, this).also {
            it.connect()
            wamChannel = it
        }
    }

    private fun closeWamChannel() {
        synchronized(channelLock) {
            try {
                wamChannel?.close()
            } catch (_: Exception) {
                // Best effort while releasing the speaker.
            }
            wamChannel = null
        }
    }

    private fun cancelIdleRelease() {
        synchronized(idleLock) {
            idleRelease?.cancel(false)
            idleRelease = null
        }
    }

    private fun scheduleIdleRelease() {
        if (destroyed) return
        synchronized(idleLock) {
            if (destroyed) return
            idleRelease?.cancel(false)
            idleRelease = idleScheduler.schedule({
                try {
                    worker.execute {
                        if (destroyed || !ownsPlayback) return@execute
                        try {
                            wamChannel?.pause()
                        } catch (_: Exception) {
                            // Closing the channel still prevents the adapter from holding resources.
                        } finally {
                            ownsPlayback = false
                            safeVolumeApplied = false
                            closeWamChannel()
                            rendererState?.transportState = "STOPPED"
                            publish("Stream ended · speaker released")
                        }
                    }
                } catch (_: RejectedExecutionException) {
                    // Service teardown won the race.
                }
            }, STREAM_RELEASE_GRACE_SECONDS, TimeUnit.SECONDS)
        }
    }

    private fun stopRenderer(removeForeground: Boolean = true) {
        cancelIdleRelease()
        rendererState?.transportState = "STOPPED"
        if (ownsPlayback) {
            try {
                wamChannel?.pause()
            } catch (_: Exception) {
                // Best effort: teardown must not keep the foreground service alive.
            }
        }
        ownsPlayback = false
        safeVolumeApplied = false
        closeWamChannel()
        try {
            renderer?.close()
        } catch (_: Exception) {
            // Best effort.
        }
        renderer = null
        rendererState = null
        running = false
        speakerIp = ""
        clientUuid = ""
        if (removeForeground) stopForeground(STOP_FOREGROUND_REMOVE)
    }

    private fun runOnWorker(action: () -> Unit) {
        if (destroyed) return
        if (Thread.currentThread().name == WORKER_THREAD_NAME) {
            action()
            return
        }

        try {
            worker.submit {
                if (!destroyed) action()
            }.get(CONTROL_ACTION_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (error: Exception) {
            throw IllegalStateException("Adapter control action failed", error)
        }
    }

    private fun dispatchWamEvent(action: () -> Unit) {
        if (destroyed) return
        try {
            worker.execute {
                if (!destroyed) action()
            }
        } catch (_: RejectedExecutionException) {
            // Service teardown won the race.
        }
    }

    override fun onPlay(rendererStreamUrl: String) = runOnWorker {
        cancelIdleRelease()
        val channel = ensureChannel()

        // Old WAM firmware may jump volume while switching into URL playback.
        // Keep it silent through SetUrlPlayback and only lift to the bounded
        // start step after the speaker has actually requested the proxy stream.
        channel.setVolumeRaw(0)
        safeVolumeApplied = false
        ownsPlayback = true
        try {
            channel.offerStream(rendererStreamUrl)
        } catch (error: Exception) {
            ownsPlayback = false
            closeWamChannel()
            throw error
        }

        rendererState?.transportState = "TRANSITIONING"
        publish("Starting playback…")
    }

    override fun onStreamOpened() = runOnWorker {
        cancelIdleRelease()
        if (ownsPlayback && !safeVolumeApplied) {
            ensureChannel().setVolumeRaw(SAFE_START_VOLUME)
            safeVolumeApplied = true
        }
        if (ownsPlayback) {
            rendererState?.transportState = "PLAYING"
            publish("Streaming player → M5")
        }
    }

    override fun onStreamClosed() {
        if (destroyed) return
        runOnWorker {
            if (ownsPlayback) scheduleIdleRelease()
        }
    }

    override fun onPause() = runOnWorker {
        cancelIdleRelease()
        if (ownsPlayback) {
            try {
                wamChannel?.pause()
            } finally {
                ownsPlayback = false
                safeVolumeApplied = false
                closeWamChannel()
            }
        } else {
            closeWamChannel()
        }
        rendererState?.transportState = "PAUSED_PLAYBACK"
        publish("Paused · speaker released")
    }

    override fun onStop() = runOnWorker {
        cancelIdleRelease()
        if (ownsPlayback) {
            try {
                wamChannel?.pause()
            } finally {
                ownsPlayback = false
                safeVolumeApplied = false
                closeWamChannel()
            }
        } else {
            closeWamChannel()
        }
        rendererState?.transportState = "STOPPED"
        publish("Stopped · speaker released")
    }

    override fun onVolume(percent: Int) = runOnWorker {
        val normalized = percent.coerceIn(0, 100)
        val raw = (normalized * 30.0 / 100.0).roundToInt().coerceIn(0, 30)
        val channel = ensureChannel()
        channel.setVolumeRaw(raw)
        safeVolumeApplied = true
        rendererState?.volumePercent = normalized
        if (!ownsPlayback) closeWamChannel()
    }

    override fun onMute(muted: Boolean) = runOnWorker {
        val channel = ensureChannel()
        channel.setMute(muted)
        rendererState?.muted = muted
        if (!ownsPlayback) closeWamChannel()
    }

    override fun onPlaybackStarted() = dispatchWamEvent {
        if (!ownsPlayback) return@dispatchWamEvent
        rendererState?.transportState = "PLAYING"
        publish("Streaming player → M5 · confirmed")
    }

    override fun onReportedError(method: String?, code: String) = dispatchWamEvent {
        val source = method?.takeIf { it.isNotBlank() }?.let { " · $it" }.orEmpty()
        rendererState?.lastError = "M5 error $code$source"
        publish("M5 reported error $code$source")
    }

    private fun promoteToForeground(message: String) {
        lastStatus = message
        startForeground(NOTIFICATION_ID, buildNotification(message))
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
                getString(R.string.app_name),
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
            .setContentTitle(getString(R.string.app_name))
            .setContentText(message)
            .setContentIntent(openPendingIntent)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "Stop", stopPendingIntent).build())
            .build()
    }

    companion object {
        const val ACTION_START = "trvny.wambridge.mobile.START"
        const val ACTION_STOP = "trvny.wambridge.mobile.STOP"
        const val PREFS = "mobile-adapter"
        const val KEY_SPEAKER_IP = "speaker_ip"
        private const val KEY_CLIENT_UUID = "wam_client_uuid"
        private const val KEY_RENDERER_UDN = "renderer_udn"
        private const val CHANNEL_ID = "wambridge-renderer"
        private const val NOTIFICATION_ID = 5101
        private const val SAFE_START_VOLUME = 3
        private const val STREAM_RELEASE_GRACE_SECONDS = 15L
        private const val DESTROY_RELEASE_TIMEOUT_MS = 1_500L
        private const val CONTROL_ACTION_TIMEOUT_MS = 5_000L
        private const val WORKER_THREAD_NAME = "wam-mobile-service"

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
