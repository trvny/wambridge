package io.github.trvny.wambridge.mobile

import android.Manifest
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

class MainActivity : Activity() {
    private lateinit var launcherButton: Button
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
            text = "WAM Bridge mobile adapter"
            textSize = 24f
        })
        layout.addView(TextView(this).apply {
            text = "\nNeutron → UPnP/DLNA → WAM Bridge → Samsung M5\n\nThe adapter is isolated under mobile/. The desktop WAM transport is not used or modified."
            textSize = 16f
        })

        speakerIp = EditText(this).apply {
            hint = "M5 IPv4 address, e.g. 10.0.0.118"
            inputType = InputType.TYPE_CLASS_PHONE
            setText(preferences.getString(RendererService.KEY_SPEAKER_IP, ""))
            setSingleLine(true)
        }
        layout.addView(speakerIp)

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
                stopService(Intent(this@MainActivity, RendererService::class.java))
                window.decorView.postDelayed({ refreshStatus() }, 250)
            }
        })

        statusView = TextView(this).apply {
            textSize = 16f
            setPadding(0, padding / 2, 0, padding)
        }
        layout.addView(statusView)

        layout.addView(TextView(this).apply {
            text = "Neutron: Settings → Output To → select ‘WAM Bridge · M5’. WAV is preferred. LPCM/L16 is also wrapped into an endless WAV stream by the adapter."
            textSize = 14f
        })

        layout.addView(Button(this).apply {
            text = "Add recovery Quick Settings tile"
            setOnClickListener { requestRecoveryTile() }
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
        preferences.edit().putString(RendererService.KEY_SPEAKER_IP, value).apply()
        return value
    }

    private fun testSpeaker() {
        val value = saveSpeakerIp() ?: return
        if (RendererService.running) {
            Toast.makeText(
                this,
                "Stop the renderer before probing. Active playback keeps one WAM control connection only.",
                Toast.LENGTH_LONG,
            ).show()
            return
        }

        statusView.text = "Testing $value…"
        Thread({
            val reachable = SamsungWamChannel.probe(value)
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
        saveSpeakerIp() ?: return
        requestNotificationPermissionIfNeeded()
        val intent = Intent(this, RendererService::class.java).apply {
            action = RendererService.ACTION_START
        }
        startForegroundService(intent)
        statusView.text = "Starting…"
        window.decorView.postDelayed({ refreshStatus() }, 750)
    }

    private fun refreshStatus() {
        statusView.text = if (RendererService.running) {
            "● ${RendererService.lastStatus}"
        } else {
            "○ ${RendererService.lastStatus}"
        }
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), NOTIFICATION_PERMISSION_REQUEST)
        }
    }

    private fun requestRecoveryTile() {
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
                if (ready) "Recovery tile ready." else "Recovery tile was not added.",
                Toast.LENGTH_SHORT,
            ).show()
        }
    }

    private fun confirmHideLauncher() {
        val recoveryReady = preferences.getBoolean("recovery_tile_ready", false)
        val message = if (recoveryReady) {
            "The launcher icon will disappear. Use the WAM Bridge Quick Settings tile to reopen this screen."
        } else {
            "The launcher icon will disappear. Add the WAM Bridge Quick Settings tile first. A running adapter also keeps a notification, but the tile is the safer way back."
        }

        AlertDialog.Builder(this)
            .setTitle("Hide launcher icon?")
            .setMessage(message)
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Hide") { _, _ -> setLauncherVisible(false) }
            .show()
    }

    private fun setLauncherVisible(visible: Boolean) {
        val launcher = ComponentName(this, "$packageName.LauncherAlias")
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
        val launcher = ComponentName(this, "$packageName.LauncherAlias")
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

    companion object {
        private const val NOTIFICATION_PERMISSION_REQUEST = 501
    }
}
