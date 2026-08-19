package io.github.trvny.wambridge.mobile

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class RadioStationsActivity : Activity() {
    private lateinit var aliasInput: EditText
    private lateinit var urlsInput: EditText
    private lateinit var statusView: TextView
    private lateinit var stationsView: LinearLayout
    private val store by lazy { RadioStationStore(this) }
    private var editingAlias: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val padding = (24 * resources.displayMetrics.density).toInt()
        val content = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }

        content.addView(TextView(this).apply {
            text = "Radio stations"
            textSize = 24f
        })
        content.addView(TextView(this).apply {
            text = "\nSave a direct HTTP/HTTPS audio stream. Put fallbacks on following lines. MP3/AAC/FLAC-style direct streams can be relayed; HLS and Ogg still need a mobile transcoder."
            textSize = 14f
        })

        aliasInput = EditText(this).apply {
            hint = "Station name"
            setSingleLine(true)
        }
        content.addView(aliasInput)

        urlsInput = EditText(this).apply {
            hint = "Primary stream URL\nFallback URL\n…"
            minLines = 3
            maxLines = 7
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI or
                InputType.TYPE_TEXT_FLAG_MULTI_LINE
        }
        content.addView(urlsInput)

        content.addView(Button(this).apply {
            text = "Save station"
            setOnClickListener { saveStation() }
        })

        content.addView(Button(this).apply {
            text = "Stop radio"
            setOnClickListener { stopRadio() }
        })

        statusView = TextView(this).apply {
            textSize = 15f
            setPadding(0, padding / 2, 0, padding / 2)
        }
        content.addView(statusView)

        stationsView = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
        }
        content.addView(stationsView)

        setContentView(ScrollView(this).apply { addView(content) })
        refreshStations()
        refreshStatus()
    }

    override fun onResume() {
        super.onResume()
        if (::statusView.isInitialized) refreshStatus()
    }

    private fun saveStation() {
        val originalAlias = editingAlias
        val result = runCatching {
            val station = store.upsert(aliasInput.text.toString(), urlsInput.text.toString().lines())
            if (originalAlias != null && !originalAlias.equals(station.alias, ignoreCase = true)) {
                store.remove(originalAlias)
            }
            station
        }
        result.fold(
            onSuccess = { station ->
                editingAlias = null
                aliasInput.text.clear()
                urlsInput.text.clear()
                statusView.text = "Saved ${station.alias}."
                refreshStations()
            },
            onFailure = { error ->
                statusView.text = error.message ?: "Could not save station"
            },
        )
    }

    private fun refreshStations() {
        stationsView.removeAllViews()
        val stations = store.all()
        if (stations.isEmpty()) {
            stationsView.addView(TextView(this).apply {
                text = "No custom stations saved."
                textSize = 14f
            })
            return
        }

        stations.forEach { station ->
            stationsView.addView(TextView(this).apply {
                text = station.alias
                textSize = 18f
                setPadding(0, 16, 0, 0)
            })
            stationsView.addView(TextView(this).apply {
                text = station.urls.joinToString("\n")
                textSize = 12f
            })
            stationsView.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(Button(this@RadioStationsActivity).apply {
                    text = "Play"
                    setOnClickListener { playStation(station) }
                })
                addView(Button(this@RadioStationsActivity).apply {
                    text = "Edit"
                    setOnClickListener {
                        editingAlias = station.alias
                        aliasInput.setText(station.alias)
                        urlsInput.setText(station.urls.joinToString("\n"))
                    }
                })
                addView(Button(this@RadioStationsActivity).apply {
                    text = "Delete"
                    setOnClickListener {
                        store.remove(station.alias)
                        if (editingAlias.equals(station.alias, ignoreCase = true)) {
                            editingAlias = null
                            aliasInput.text.clear()
                            urlsInput.text.clear()
                        }
                        refreshStations()
                    }
                })
            })
        }
    }

    private fun playStation(station: MobileRadioStation) {
        val target = getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
            .getString(RendererService.KEY_SPEAKER_IP, "")
            .orEmpty()
            .trim()
        if (!RendererService.isReasonableIpv4(target)) {
            statusView.text = "Configure the M5 address in WAM Bridge first."
            return
        }

        startForegroundService(
            Intent(this, RadioService::class.java).apply {
                action = RadioService.ACTION_PLAY
                putExtra(RadioService.EXTRA_ALIAS, station.alias)
            },
        )
        statusView.text = "Starting ${station.alias}…"
        window.decorView.postDelayed({ refreshStatus() }, 900)
    }

    private fun stopRadio() {
        startService(
            Intent(this, RadioService::class.java).apply {
                action = RadioService.ACTION_STOP
            },
        )
        statusView.text = "Stopping radio…"
        window.decorView.postDelayed({ refreshStatus() }, 300)
    }

    private fun refreshStatus() {
        statusView.text = if (RadioService.running) {
            "● ${RadioService.lastStatus}"
        } else {
            "○ ${RadioService.lastStatus}"
        }
    }
}
