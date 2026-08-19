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
        content.addView(refreshButton)

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
        refreshButton.isEnabled = false
        presetsView.removeAllViews()
        statusView.text = "Reading TuneIn presets from $target…"

        Thread({
            val result = runCatching {
                releaseRenderer()
                SamsungTuneIn.getPresets(applicationContext, target)
            }
            runOnUiThread {
                refreshButton.isEnabled = true
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
                releaseRenderer()
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

    private fun setButtonsEnabled(enabled: Boolean) {
        refreshButton.isEnabled = enabled
        for (index in 0 until presetsView.childCount) {
            presetsView.getChildAt(index).isEnabled = enabled
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

    companion object {
        private const val RENDERER_STOP_TIMEOUT_MS = 2_500L
    }
}
