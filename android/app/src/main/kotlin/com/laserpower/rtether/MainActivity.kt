package com.laserpower.rtether

import android.Manifest
import android.content.Intent
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/**
 * One Start/Stop button. Also the entry point the PC relay uses:
 * `am start -n com.laserpower.rtether/.MainActivity -a com.laserpower.rtether.START`
 * (the adb shell user may not start the VPN service directly, but this activity may).
 */
class MainActivity : AppCompatActivity() {

    private lateinit var status: TextView
    private lateinit var toggle: Button
    private var finishAfterConsent = false

    private val vpnConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) sendToService(TetherVpnService.ACTION_START)
            if (finishAfterConsent) finish()
        }

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        toggle = findViewById(R.id.toggle)

        toggle.setOnClickListener {
            if (TetherVpnService.state.value == Tunnel.STATE_STOPPED) start() else stop()
        }

        lifecycleScope.launch {
            TetherVpnService.state.collect { value ->
                status.text = value
                toggle.text = getString(
                    if (value == Tunnel.STATE_STOPPED) R.string.start else R.string.stop,
                )
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        handleRelayIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleRelayIntent(intent)
    }

    /** Forwards START/STOP sent by the relay over adb; closes again unless consent is needed. */
    private fun handleRelayIntent(intent: Intent?) {
        when (intent?.action) {
            TetherVpnService.ACTION_START -> {
                val consent = VpnService.prepare(this)
                if (consent != null) {
                    finishAfterConsent = true
                    vpnConsent.launch(consent)
                } else {
                    sendToService(TetherVpnService.ACTION_START)
                    finish()
                }
            }
            TetherVpnService.ACTION_STOP -> {
                sendToService(TetherVpnService.ACTION_STOP)
                finish()
            }
        }
    }

    private fun start() {
        val consent = VpnService.prepare(this)
        if (consent != null) {
            finishAfterConsent = false
            vpnConsent.launch(consent)
        } else {
            sendToService(TetherVpnService.ACTION_START)
        }
    }

    private fun stop() = sendToService(TetherVpnService.ACTION_STOP)

    private fun sendToService(action: String) {
        val intent = Intent(this, TetherVpnService::class.java).setAction(action)
        ContextCompat.startForegroundService(this, intent)
    }
}
