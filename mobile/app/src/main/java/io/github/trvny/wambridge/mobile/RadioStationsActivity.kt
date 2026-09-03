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
    private lateinit var editorCard: LinearLayout
    private lateinit var scrollView: ScrollView
    private val store by lazy { RadioStationStore(this) }
    private var editingAlias: String? = null

    // Last value read from the speaker, or null when it has never answered. Kept so a
    // step lands next to the truth rather than next to whatever was last displayed.
    private var volumeStep: Int? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        MobileUi.applyWindow(this)

        val content = MobileUi.page(this)
        content.addView(
            MobileUi.header(
                this,
                "Radio stations",
                "Saved streams with TuneIn resolution and ordered fallbacks.",
            ),
        )

        statusView = MobileUi.status(this)
        content.addView(statusView)

        content.addView(MobileUi.sectionTitle(this, "Playback"))
        val playbackCard = MobileUi.card(this)
        playbackCard.addView(MobileUi.body(this, "Direct MP3/AAC/FLAC-style streams are relayed by the phone. HLS and Ogg still need transcoding."))
        playbackCard.addView(MobileUi.row(this).apply {
            setPadding(0, MobileUi.dp(this@RadioStationsActivity, 12), 0, 0)
            MobileUi.addWeighted(this, MobileUi.button(this@RadioStationsActivity, "Stop radio", MobileUi.ButtonKind.DANGER) { stopRadio() }, 1.3f)
            MobileUi.addWeighted(this, MobileUi.button(this@RadioStationsActivity, "−") { stepVolume(-1) }, 0.65f)
            volumeView = TextView(this@RadioStationsActivity).apply {
                text = "Volume …"
                textSize = 14f
                gravity = android.view.Gravity.CENTER
                setTextColor(getColor(R.color.wam_text))
            }
            MobileUi.addWeighted(this, volumeView, 1.2f)
            MobileUi.addWeighted(this, MobileUi.button(this@RadioStationsActivity, "+") { stepVolume(+1) }, 0.65f, marginDp = 0)
        })
        content.addView(playbackCard)

        content.addView(MobileUi.sectionTitle(this, "Add or edit"))
        editorCard = MobileUi.card(this)
        editorCard.addView(MobileUi.label(this, "Station name"))
        aliasInput = MobileUi.field(this, "e.g. Radio Paradise").apply { setSingleLine(true) }
        editorCard.addView(aliasInput)
        editorCard.addView(MobileUi.label(this, "Stream URLs").apply { setPadding(MobileUi.dp(this@RadioStationsActivity, 2), MobileUi.dp(this@RadioStationsActivity, 12), 0, MobileUi.dp(this@RadioStationsActivity, 6)) })
        urlsInput = MobileUi.field(this, "Primary URL\nFallback URL\n…", multiline = true).apply {
            minLines = 3
            maxLines = 7
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI or InputType.TYPE_TEXT_FLAG_MULTI_LINE
        }
        editorCard.addView(urlsInput)
        editorCard.addView(MobileUi.label(this, "TuneIn ID, optional").apply { setPadding(MobileUi.dp(this@RadioStationsActivity, 2), MobileUi.dp(this@RadioStationsActivity, 12), 0, MobileUi.dp(this@RadioStationsActivity, 6)) })
        tuneInInput = MobileUi.field(this, "e.g. s15984").apply { setSingleLine(true) }
        editorCard.addView(tuneInInput)
        editorCard.addView(MobileUi.button(this, "Save station", MobileUi.ButtonKind.PRIMARY) { saveStation() }.apply {
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            ).apply { topMargin = MobileUi.dp(this@RadioStationsActivity, 12) }
        })
        content.addView(editorCard)

        content.addView(MobileUi.sectionTitle(this, "Saved stations"))
        stationsView = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        content.addView(stationsView)

        scrollView = ScrollView(this).apply { addView(content) }
        setContentView(scrollView)
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
            stationsView.addView(MobileUi.body(this, "No stations saved yet."))
            return
        }

        stations.forEach { station ->
            val card = MobileUi.card(this)
            card.addView(TextView(this).apply {
                text = station.alias
                textSize = 18f
                typeface = android.graphics.Typeface.DEFAULT_BOLD
                setTextColor(getColor(R.color.wam_text))
            })
            val detail = buildList {
                station.tuneInId?.let { add("TuneIn $it") }
                addAll(station.urls)
            }.joinToString("\n")
            card.addView(MobileUi.body(this, detail).apply {
                maxLines = 3
                setPadding(0, MobileUi.dp(this@RadioStationsActivity, 4), 0, MobileUi.dp(this@RadioStationsActivity, 12))
            })
            card.addView(MobileUi.row(this).apply {
                MobileUi.addWeighted(this, MobileUi.button(this@RadioStationsActivity, "Play", MobileUi.ButtonKind.PRIMARY) { playStation(station) })
                MobileUi.addWeighted(this, MobileUi.button(this@RadioStationsActivity, "Edit") {
                    editingAlias = station.alias
                    aliasInput.setText(station.alias)
                    urlsInput.setText(station.urls.joinToString("\n"))
                    tuneInInput.setText(station.tuneInId.orEmpty())
                    statusView.text = "Editing ${station.alias}"
                    editorCard.post { scrollView.smoothScrollTo(0, editorCard.top) }
                })
                MobileUi.addWeighted(this, MobileUi.button(this@RadioStationsActivity, "Delete", MobileUi.ButtonKind.DANGER) {
                    store.remove(station.alias)
                    if (editingAlias.equals(station.alias, ignoreCase = true)) {
                        editingAlias = null
                        aliasInput.text.clear()
                        urlsInput.text.clear()
                        tuneInInput.text.clear()
                    }
                    refreshStations()
                }, marginDp = 0)
            })
            stationsView.addView(card)
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
