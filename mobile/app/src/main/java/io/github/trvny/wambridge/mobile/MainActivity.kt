package io.github.trvny.wambridge.mobile

import android.app.Activity
import android.app.AlertDialog
import android.app.StatusBarManager
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.drawable.Icon
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/**
 * The launcher alias, spelled as `AndroidManifest.xml` declares it.
 *
 * `LauncherAliasNameTest` pins the two together, because nothing else would notice them drifting:
 * `setComponentEnabledSetting` on a component that does not exist is a silent no-op.
 */
internal const val LAUNCHER_ALIAS_CLASS = "trvny.wambridge.mobile.LauncherAlias"

class MainActivity : Activity() {
    private lateinit var launcherButton: Button
    private lateinit var discoverButton: Button
    private lateinit var speakerIp: EditText
    private lateinit var statusView: TextView

    private val preferences by lazy {
        getSharedPreferences(RendererService.PREFS, MODE_PRIVATE)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val padding = (24 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(padding, padding, padding, padding)
        }

        layout.addView(TextView(this).apply {
            text = getString(R.string.app_name)
            textSize = 24f
        })
        layout.addView(TextView(this).apply {
            text = "\nUPnP/DLNA player → WAM Bridge → Samsung M5"
            textSize = 16f
        })

        speakerIp = EditText(this).apply {
            hint = "M5 IPv4 address, e.g. 10.0.0.118"
            inputType = InputType.TYPE_CLASS_PHONE
            setText(preferences.getString(RendererService.KEY_SPEAKER_IP, ""))
            setSingleLine(true)
        }
        layout.addView(speakerIp)

        discoverButton = Button(this).apply {
            text = "Discover WAM speaker"
            setOnClickListener { discoverSpeaker(allowScan = true) }
        }
        layout.addView(discoverButton)

        layout.addView(Button(this).apply {
            text = "Save + test M5"
            setOnClickListener { testSpeaker() }
        })

        layout.addView(Button(this).apply {
            text = "Start UPnP renderer"
            setOnClickListener { startRenderer() }
        })

        layout.addView(Button(this).apply {
            text = "Stop renderer"
            setOnClickListener {
                startService(
                    Intent(this@MainActivity, RendererService::class.java).apply {
                        action = RendererService.ACTION_STOP
                    },
                )
                refreshUntilSettled()
            }
        })

        statusView = TextView(this).apply {
            textSize = 16f
            setPadding(0, padding / 2, 0, padding)
        }
        layout.addView(statusView)

        layout.addView(Button(this).apply {
            text = "TuneIn presets"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, TuneInActivity::class.java))
            }
        })

        layout.addView(Button(this).apply {
            text = "TuneIn catalogue"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, CatalogueActivity::class.java))
            }
        })

        layout.addView(Button(this).apply {
            text = "Radio stations"
            setOnClickListener {
                startActivity(Intent(this@MainActivity, RadioStationsActivity::class.java))
            }
        })

        layout.addView(TextView(this).apply {
            text = "Neutron: Settings → Output To → select ‘WAM Bridge · M5’. Other local UPnP/DLNA players can use the same renderer; WAV/L16, MP3 and FLAC are advertised."
            textSize = 14f
        })

        layout.addView(Button(this).apply {
            text = "Add Quick Settings toggle"
            setOnClickListener { requestQuickSettingsTile() }
        })

        launcherButton = Button(this).apply {
            setOnClickListener {
                if (isLauncherHidden()) {
                    setLauncherVisible(true)
                } else {
                    confirmHideLauncher()
                }
            }
        }
        layout.addView(launcherButton)

        setContentView(ScrollView(this).apply { addView(layout) })
        refreshLauncherButton()
        refreshStatus()

        if (!RendererService.isReasonableIpv4(speakerIp.text.toString().trim())) {
            window.decorView.post { discoverSpeaker(allowScan = false) }
        }
    }

    override fun onResume() {
        super.onResume()
        if (::statusView.isInitialized) refreshStatus()
    }

    private fun saveSpeakerIp(): String? {
        val value = speakerIp.text.toString().trim()
        if (!RendererService.isReasonableIpv4(value)) {
            speakerIp.error = "Enter an IPv4 address"
            return null
        }
        SpeakerTarget.rememberManualIp(applicationContext, value)
        return value
    }

    private fun discoverSpeaker(allowScan: Boolean) {
        if (RendererService.busy) {
            Toast.makeText(this, "Stop the renderer before discovery.", Toast.LENGTH_SHORT).show()
            return
        }

        discoverButton.isEnabled = false
        statusView.text = if (allowScan) {
            "Discovering WAM speakers on Wi-Fi…"
        } else {
            "Looking for WAM speakers on Wi-Fi…"
        }

        Thread({
            val result = WamDiscovery.discover(applicationContext, allowScan = allowScan)
            val speakers = result.speakers
            runOnUiThread {
                discoverButton.isEnabled = true
                when {
                    speakers.isEmpty() && allowScan -> {
                        statusView.text = emptyScanMessage(result.scan)
                    }

                    speakers.isEmpty() -> {
                        statusView.text = "No WAM speaker announced via SSDP. Tap Discover for LAN fallback or enter the IP manually."
                    }

                    speakers.size == 1 -> useDiscoveredSpeaker(speakers.single())
                    else -> chooseDiscoveredSpeaker(speakers)
                }
            }
        }, "wam-mobile-discovery").start()
    }

    /**
     * What to say when the sweep came back empty. Only a full sweep licenses
     * "not found"; a narrowed one has to admit it did not look, or the reader
     * goes hunting for a network problem that is not there.
     */
    private fun emptyScanMessage(scan: WamDiscovery.Scan): String = when (scan) {
        is WamDiscovery.Scan.Narrowed ->
            "Scanned ${scan.hosts} addresses around this phone. This Wi-Fi is a /${scan.prefixLength} " +
                "(${scan.subnetHosts} addresses), too wide to sweep, so a speaker outside that range " +
                "was not checked. Enter the IP manually."

        is WamDiscovery.Scan.Overlapping ->
            "Scanned ${scan.hosts} addresses, but ${scan.shared} of them exist on more than one " +
                "Wi-Fi network here and were checked on only one. Enter the IP manually."

        WamDiscovery.Scan.NoAddresses ->
            "This Wi-Fi has no other addresses to scan. Enter the speaker IP manually."

        WamDiscovery.Scan.NotRun ->
            "No Wi-Fi network available for discovery. Enter the speaker IP manually."

        is WamDiscovery.Scan.Full ->
            "No WAM speaker found on any of the ${scan.hosts} addresses on this Wi-Fi. " +
                "Enter the IP manually if discovery is blocked by the network."
    }

    private fun chooseDiscoveredSpeaker(speakers: List<WamDiscovery.Speaker>) {
        val labels = speakers.map { "${it.ip} · ${it.source}" }.toTypedArray()
        AlertDialog.Builder(this)
            .setTitle("Choose WAM speaker")
            .setItems(labels) { _, which -> useDiscoveredSpeaker(speakers[which]) }
            .setNegativeButton("Cancel", null)
            .show()
        statusView.text = "Found ${speakers.size} WAM speakers."
    }

    private fun useDiscoveredSpeaker(speaker: WamDiscovery.Speaker) {
        speakerIp.setText(speaker.ip)
        SpeakerTarget.rememberManualIp(applicationContext, speaker.ip)
        statusView.text = "Found WAM speaker at ${speaker.ip} via ${speaker.source}."
    }

    private fun testSpeaker() {
        val value = saveSpeakerIp() ?: return
        if (RendererService.busy) {
            Toast.makeText(
                this,
                "Stop the renderer before probing. Active playback keeps one WAM control connection only.",
                Toast.LENGTH_LONG,
            ).show()
            return
        }

        statusView.text = "Testing $value…"
        Thread({
            val reachable = SamsungWamChannel.probe(applicationContext, value)
            runOnUiThread {
                statusView.text = if (reachable) {
                    "M5 answered at $value."
                } else {
                    "No WAM response from $value."
                }
            }
        }, "wam-mobile-probe").start()
    }

    private fun startRenderer() {
        val manualTarget = speakerIp.text.toString().trim()
        if (manualTarget.isNotEmpty()) {
            if (!RendererService.isReasonableIpv4(manualTarget)) {
                speakerIp.error = "Enter an IPv4 address or leave it empty for auto-discovery"
                return
            }
            SpeakerTarget.rememberManualIp(applicationContext, manualTarget)
        }
        val intent = Intent(this, RendererService::class.java).apply {
            action = RendererService.ACTION_START
        }
        startForegroundService(intent)
        statusView.text = "Finding M5 and starting renderer…"
        refreshUntilSettled()
    }

    private fun refreshUntilSettled(minimumPolls: Int = 2) {
        refreshStatus()
        if (minimumPolls > 0 || RendererService.transitioning) {
            window.decorView.postDelayed(
                { refreshUntilSettled((minimumPolls - 1).coerceAtLeast(0)) },
                250,
            )
        }
    }

    private fun refreshStatus() {
        val marker = when (RendererService.phase) {
            RendererService.Phase.RUNNING -> "●"
            RendererService.Phase.STARTING, RendererService.Phase.STOPPING -> "◐"
            RendererService.Phase.STOPPED -> "○"
        }
        statusView.text = "$marker ${RendererService.lastStatus}"
    }

    private fun requestQuickSettingsTile() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            Toast.makeText(
                this,
                "Add the WAM Bridge tile manually from Android Quick Settings before hiding the launcher icon.",
                Toast.LENGTH_LONG,
            ).show()
            return
        }

        val statusBar = getSystemService(StatusBarManager::class.java)
        val component = ComponentName(this, WamBridgeTileService::class.java)
        val icon = Icon.createWithResource(this, R.drawable.ic_qs_tile)

        statusBar.requestAddTileService(
            component,
            getString(R.string.app_name),
            icon,
            mainExecutor,
        ) { result ->
            val ready = result == StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ADDED ||
                result == StatusBarManager.TILE_ADD_REQUEST_RESULT_TILE_ALREADY_ADDED
            preferences.edit().putBoolean("recovery_tile_ready", ready).apply()
            Toast.makeText(
                this,
                if (ready) "Quick Settings toggle ready." else "Quick Settings tile was not added.",
                Toast.LENGTH_SHORT,
            ).show()
        }
    }

    private fun confirmHideLauncher() {
        val recoveryReady = preferences.getBoolean("recovery_tile_ready", false)
        val message = if (recoveryReady) {
            "The launcher icon will disappear. Tap the WAM Bridge Quick Settings tile to start/stop the renderer; long-press it to reopen this screen."
        } else {
            "The launcher icon will disappear. Add the WAM Bridge Quick Settings tile first. Its long-press opens this screen even after the launcher icon is hidden."
        }

        AlertDialog.Builder(this)
            .setTitle("Hide launcher icon?")
            .setMessage(message)
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Hide") { _, _ -> setLauncherVisible(false) }
            .show()
    }

    /**
     * The launcher alias, named exactly as the manifest declares it.
     *
     * **Do not rebuild this from `packageName`.** The alias is declared with an absolute name, so
     * it stays `trvny.wambridge.mobile.LauncherAlias` in every variant, while `packageName` is the
     * applicationId and a debug build suffixes that to `trvny.wambridge.mobile.debug`. Deriving one
     * from the other pointed the hide/show control at a component that does not exist, and
     * `setComponentEnabledSetting` on a missing component is a silent no-op - the button would have
     * looked fine and done nothing. Caught in review on the commit that added the suffix.
     */
    private fun launcherAliasComponent(): ComponentName =
        ComponentName(this, LAUNCHER_ALIAS_CLASS)

    private fun setLauncherVisible(visible: Boolean) {
        val launcher = launcherAliasComponent()
        packageManager.setComponentEnabledSetting(
            launcher,
            if (visible) {
                PackageManager.COMPONENT_ENABLED_STATE_DEFAULT
            } else {
                PackageManager.COMPONENT_ENABLED_STATE_DISABLED
            },
            PackageManager.DONT_KILL_APP,
        )

        Toast.makeText(
            this,
            if (visible) "Launcher icon restored." else "Launcher icon hidden.",
            Toast.LENGTH_SHORT,
        ).show()
        refreshLauncherButton()
    }

    private fun isLauncherHidden(): Boolean {
        val launcher = launcherAliasComponent()
        return packageManager.getComponentEnabledSetting(launcher) ==
            PackageManager.COMPONENT_ENABLED_STATE_DISABLED
    }

    private fun refreshLauncherButton() {
        launcherButton.text = if (isLauncherHidden()) {
            "Restore launcher icon"
        } else {
            "Hide launcher icon"
        }
    }
}
