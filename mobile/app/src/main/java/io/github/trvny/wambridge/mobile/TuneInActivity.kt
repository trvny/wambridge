package io.github.trvny.wambridge.mobile

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.SystemClock
import android.widget.Button
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class TuneInActivity : Activity() {
    private lateinit var statusView: TextView
    private lateinit var refreshButton: Button
    private lateinit var stopButton: Button
    private lateinit var presetsView: LinearLayout

    private val preferences by lazy {
        getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val padding = (24 * resources.displayMetrics.density).toInt()
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }

        content.addView(TextView(this).apply {
            text = "TuneIn presets"
            textSize = 24f
        })
        content.addView(TextView(this).apply {
            text = "\nNative presets stored by the Samsung speaker. WAM Bridge reads and starts them; editing the TuneIn account still belongs to Samsung's plugin."
            textSize = 14f
        })

        refreshButton = Button(this).apply {
            text = "Refresh presets"
            setOnClickListener { loadPresets() }
        }
        stopButton = Button(this).apply {
            text = "Stop"
            setOnClickListener { stopPlayback() }
        }
        // Both controls sit at the top so Stop stays reachable above a long preset list.
        content.addView(
            LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(refreshButton)
                addView(stopButton)
            },
        )

        statusView = TextView(this).apply {
            textSize = 15f
            setPadding(0, padding / 2, 0, padding / 2)
        }
        content.addView(statusView)

        presetsView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        content.addView(presetsView)

        setContentView(ScrollView(this).apply { addView(content) })
        loadPresets()
    }

    private fun speakerIp(): String? {
        val value = preferences.getString(RendererService.KEY_SPEAKER_IP, "").orEmpty().trim()
        if (!RendererService.isReasonableIpv4(value)) {
            statusView.text = "Configure the M5 address in WAM Bridge first."
            return null
        }
        return value
    }

    private fun loadPresets() {
        val target = speakerIp() ?: return
        setButtonsEnabled(false)
        presetsView.removeAllViews()
        statusView.text = "Reading TuneIn presets from $target…"

        Thread({
            val result = runCatching {
                releasePlaybackOwners()
                SamsungTuneIn.getPresets(applicationContext, target)
            }
            runOnUiThread {
                setButtonsEnabled(true)
                result.fold(
                    onSuccess = ::showPresets,
                    onFailure = { error ->
                        statusView.text = "Could not read TuneIn presets: ${error.message ?: error.javaClass.simpleName}"
                    },
                )
            }
        }, "wam-mobile-tunein-list").start()
    }

    private fun showPresets(presets: List<SamsungTuneIn.Preset>) {
        presetsView.removeAllViews()
        if (presets.isEmpty()) {
            statusView.text = "The speaker returned no TuneIn presets."
            return
        }
        statusView.text = "${presets.size} TuneIn preset${if (presets.size == 1) "" else "s"}."

        presets.forEach { preset ->
            presetsView.addView(Button(this).apply {
                text = "${preset.contentId} · ${preset.title}"
                isAllCaps = false
                setOnClickListener { playPreset(preset) }
            })
            preset.description?.takeIf { it.isNotBlank() }?.let { description ->
                presetsView.addView(TextView(this).apply {
                    text = description
                    textSize = 13f
                })
            }
        }
    }

    private fun playPreset(preset: SamsungTuneIn.Preset) {
        val target = speakerIp() ?: return
        setButtonsEnabled(false)
        statusView.text = "Starting ${preset.title}…"

        Thread({
            val result = runCatching {
                releasePlaybackOwners()
                SamsungTuneIn.playSafely(applicationContext, target, preset)
            }
            runOnUiThread {
                setButtonsEnabled(true)
                result.fold(
                    onSuccess = {
                        statusView.text = "Playing TuneIn · ${preset.title} · volume 3"
                    },
                    onFailure = { error ->
                        statusView.text = "TuneIn start failed; speaker left muted for safety: ${error.message ?: error.javaClass.simpleName}"
                    },
                )
            }
        }, "wam-mobile-tunein-play").start()
    }

    private fun stopPlayback() {
        val target = speakerIp() ?: return
        setButtonsEnabled(false)
        statusView.text = "Stopping playback on $target…"

        Thread({
            val result = runCatching {
                releasePlaybackOwners()
                endSpeakerPlayback(target)
            }
            runOnUiThread {
                setButtonsEnabled(true)
                result.fold(
                    onSuccess = { report -> statusView.text = report },
                    onFailure = { error ->
                        statusView.text = "Could not stop playback: ${error.message ?: error.javaClass.simpleName}"
                    },
                )
            }
        }, "wam-mobile-tunein-stop").start()
    }

    private fun endSpeakerPlayback(target: String): String {
        val clientUuid = preferences.getString(KEY_CLIENT_UUID, null)
            ?: SamsungWamChannel.newClientUuid().also {
                preferences.edit().putString(KEY_CLIENT_UUID, it).apply()
            }
        val channel = SamsungWamChannel(applicationContext, target, clientUuid)
        val confirmDeadline = try {
            channel.connect()
            // Not a redundant pair, so do not collapse it into one call: SetFunc aimed at
            // wifi while the speaker is already on wifi does nothing at all - it is told to
            // become what it already is. Ending a preset takes the detour through another
            // source, which also lands back in submode=dlna, the idle state the rest of this
            // app expects. Measured on the M5 on 2026-08-19, where SetPlaybackControl stop
            // was refused on both CPM ("Current track token is empty.") and UIC (result="ng").
            channel.selectFunction("bt")
            Thread.sleep(FUNCTION_SWITCH_PAUSE_MS)
            channel.selectFunction("wifi")
            val deadline = SystemClock.elapsedRealtime() + STOP_CONFIRM_TIMEOUT_MS
            // Sends are fire-and-forget here, so let the last one leave before the socket
            // closes. Waiting for the speaker to settle is the poll's job below.
            Thread.sleep(FUNCTION_SWITCH_PAUSE_MS)
            deadline
        } finally {
            channel.close()
        }

        // A write that left the phone is not a stop. Neither SetFunc is acknowledged to
        // the caller on this channel, so the state is read back and only what GetFunc
        // actually answered gets reported.
        //
        // Read it repeatedly, not once: the M5 leaves cp between two and three seconds
        // after SetFunc wifi (measured 2026-08-20), so a single read at a fixed delay
        // lands on submode=cp and calls a stop that worked unconfirmed.
        var lastRead: SamsungWamChannel.FunctionState? = null
        var lastError: Throwable? = null
        var stopped = false
        while (true) {
            val attempt = runCatching { SamsungWamChannel.readFunction(applicationContext, target) }
            val read = attempt.getOrNull()
            if (read == null) {
                // A read that failed is not a verdict; keep asking until the deadline.
                lastError = attempt.exceptionOrNull()
            } else {
                lastRead = read
                stopped = read.function.equals("wifi", ignoreCase = true) &&
                    !read.submode.isNullOrBlank() &&
                    !read.submode.equals("cp", ignoreCase = true)
            }
            if (stopped) break
            val remaining = confirmDeadline - SystemClock.elapsedRealtime()
            if (remaining <= 0) break
            Thread.sleep(minOf(STOP_POLL_INTERVAL_MS, remaining))
        }

        val finalRead = lastRead
        val reading = if (finalRead != null) {
            "function=${finalRead.function.orEmpty().ifBlank { "?" }} · " +
                "submode=${finalRead.submode.orEmpty().ifBlank { "(empty)" }}"
        } else {
            val error = lastError
            "GetFunc did not answer: ${error?.message ?: error?.javaClass?.simpleName ?: "no reply"}"
        }
        return if (stopped) {
            "Playback stopped · $reading"
        } else {
            "SetFunc bt/wifi sent, but the stop could not be confirmed · $reading"
        }
    }

    private fun setButtonsEnabled(enabled: Boolean) {
        refreshButton.isEnabled = enabled
        stopButton.isEnabled = enabled
        for (index in 0 until presetsView.childCount) {
            presetsView.getChildAt(index).isEnabled = enabled
        }
    }

    private fun releasePlaybackOwners() {
        releaseRenderer()
        releaseRadio()
    }

    private fun releaseRenderer() {
        if (!RendererService.running) return
        startService(
            Intent(this, RendererService::class.java).apply {
                action = RendererService.ACTION_STOP
            },
        )
        val deadline = SystemClock.elapsedRealtime() + OWNER_STOP_TIMEOUT_MS
        while (RendererService.running && SystemClock.elapsedRealtime() < deadline) {
            Thread.sleep(50)
        }
        check(!RendererService.running) { "Renderer did not release the WAM control channel" }
    }

    private fun releaseRadio() {
        if (!RadioService.running) return
        startService(
            Intent(this, RadioService::class.java).apply {
                action = RadioService.ACTION_STOP
            },
        )
        val deadline = SystemClock.elapsedRealtime() + OWNER_STOP_TIMEOUT_MS
        while (RadioService.running && SystemClock.elapsedRealtime() < deadline) {
            Thread.sleep(50)
        }
        check(!RadioService.running) { "Radio did not release the WAM control channel" }
    }

    companion object {
        private const val OWNER_STOP_TIMEOUT_MS = 2_500L

        /** Gap between `SetFunc bt` and `SetFunc wifi`, and the flush after the last send. */
        private const val FUNCTION_SWITCH_PAUSE_MS = 2_000L

        /** How long to keep asking `GetFunc` whether the speaker has left `cp`. */
        private const val STOP_CONFIRM_TIMEOUT_MS = 8_000L

        /** Gap between those reads - this firmware wedges under rapid calls. */
        private const val STOP_POLL_INTERVAL_MS = 1_000L

        private const val KEY_CLIENT_UUID = "tunein_client_uuid"
    }
}
