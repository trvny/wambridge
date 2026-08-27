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
    private lateinit var tuneInInput: EditText
    private lateinit var statusView: TextView
    private lateinit var volumeView: TextView
    private lateinit var stationsView: LinearLayout
    private val store by lazy { RadioStationStore(this) }
    private var editingAlias: String? = null

    // Last value read from the speaker, or null when it has never answered. Kept so a
    // step lands next to the truth rather than next to whatever was last displayed.
    private var volumeStep: Int? = null

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
            text = "\nSave a direct HTTP/HTTPS audio stream. Put fallbacks on following lines. MP3/AAC/FLAC-style direct streams can be relayed; HLS and Ogg still need a mobile transcoder.\n\nA TuneIn station id like s15984 is optional. It is looked up again every time the station plays, so it keeps working after the broadcaster moves its stream; the saved URLs stay behind it as fallbacks."
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

        tuneInInput = EditText(this).apply {
            hint = "TuneIn station id (optional, e.g. s15984)"
            setSingleLine(true)
        }
        content.addView(tuneInInput)

        content.addView(Button(this).apply {
            text = "Save station"
            setOnClickListener { saveStation() }
        })

        content.addView(Button(this).apply {
            text = "Stop radio"
            setOnClickListener { stopRadio() }
        })

        // The M5 has thirty volume steps, not a hundred. Stepping the raw scale means every
        // press moves the speaker; a 0-100 slider divided down would ignore two presses in
        // three, which is exactly how this speaker feels from a phone player over UPnP.
        content.addView(
            LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                addView(Button(this@RadioStationsActivity).apply {
                    text = "Vol −"
                    setOnClickListener { stepVolume(-1) }
                })
                addView(Button(this@RadioStationsActivity).apply {
                    text = "Vol +"
                    setOnClickListener { stepVolume(+1) }
                })
                volumeView = TextView(this@RadioStationsActivity).apply {
                    textSize = 15f
                    setPadding(padding, 0, 0, 0)
                    text = "volume …"
                }
                addView(volumeView)
            },
        )

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
        // Re-read rather than trust the last shown value: the speaker's volume can be
        // changed from its own buttons, from foobar or from another app while this screen
        // is away, and a stale reading would make the next press jump.
        if (::volumeView.isInitialized) refreshVolume()
    }

    private fun saveStation() {
        val originalAlias = editingAlias
        val result = runCatching {
            val station = store.upsert(
                aliasInput.text.toString(),
                urlsInput.text.toString().lines(),
                tuneInInput.text.toString(),
            )
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
                tuneInInput.text.clear()
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
                val tuneInLine = station.tuneInId?.let { "TuneIn $it" }
                text = (listOfNotNull(tuneInLine) + station.urls).joinToString("\n")
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
                        tuneInInput.setText(station.tuneInId.orEmpty())
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
                            tuneInInput.text.clear()
                        }
                        refreshStations()
                    }
                })
            })
        }
    }

    private fun playStation(station: MobileRadioStation) {
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

    /** Stable per-screen client identity, in the same shape the other screens keep theirs. */
    private fun clientUuid(): String {
        val preferences = getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
        return preferences.getString(KEY_CLIENT_UUID, null)
            ?: SamsungWamChannel.newClientUuid().also {
                preferences.edit().putString(KEY_CLIENT_UUID, it).apply()
            }
    }

    private fun speakerAddress(): String? {
        val target = getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
            .getString(RendererService.KEY_SPEAKER_IP, "")
            .orEmpty()
            .trim()
        return if (RendererService.isReasonableIpv4(target)) target else null
    }

    /** Read the speaker's volume so the buttons start from its value, not from a guess. */
    private fun refreshVolume() {
        val target = speakerAddress() ?: run {
            volumeView.text = "volume —"
            return
        }
        val appContext = applicationContext
        Thread({
            val read = runCatching { SamsungWamChannel.readVolumeRaw(appContext, target) }
            runOnUiThread {
                read.fold(
                    onSuccess = { step ->
                        volumeStep = step
                        volumeView.text = "volume $step/${SamsungWamChannel.MAX_VOLUME_STEP}"
                    },
                    onFailure = {
                        volumeStep = null
                        volumeView.text = "volume ?"
                    },
                )
            }
        }, "wam-radio-volume-read").start()
    }

    private fun stepVolume(delta: Int) {
        val target = speakerAddress() ?: run {
            statusView.text = "Configure the M5 address in WAM Bridge first."
            return
        }
        val appContext = applicationContext
        Thread({
            // Re-read before stepping when the current value is unknown, so a press cannot
            // jump the speaker somewhere unintended - it has no volume-relative command.
            val current = volumeStep
                ?: runCatching { SamsungWamChannel.readVolumeRaw(appContext, target) }.getOrNull()
            if (current == null) {
                runOnUiThread {
                    volumeView.text = "volume ?"
                    statusView.text = "Could not read the speaker volume."
                }
                return@Thread
            }
            val wanted = (current + delta)
                .coerceIn(SamsungWamChannel.MIN_VOLUME_STEP, SamsungWamChannel.MAX_VOLUME_STEP)
            val applied = runCatching {
                val channel = SamsungWamChannel(appContext, target, clientUuid())
                try {
                    channel.connect()
                    channel.setVolumeRaw(wanted)
                } finally {
                    channel.close()
                }
            }
            runOnUiThread {
                applied.fold(
                    onSuccess = {
                        volumeStep = wanted
                        volumeView.text = "volume $wanted/${SamsungWamChannel.MAX_VOLUME_STEP}"
                    },
                    onFailure = { error ->
                        statusView.text =
                            "Could not set volume: ${error.message ?: error.javaClass.simpleName}"
                    },
                )
            }
        }, "wam-radio-volume-step").start()
    }

    private fun refreshStatus() {
        statusView.text = if (RadioService.running) {
            "● ${RadioService.lastStatus}"
        } else {
            "○ ${RadioService.lastStatus}"
        }
    }

    companion object {
        private const val KEY_CLIENT_UUID = "radio_stations_client_uuid"
    }
}
