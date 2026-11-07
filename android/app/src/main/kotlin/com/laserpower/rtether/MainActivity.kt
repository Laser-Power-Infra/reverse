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

class MainActivity : AppCompatActivity() {

    private lateinit var status: TextView
    private lateinit var toggle: Button

    private val vpnConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) sendToService(TetherVpnService.ACTION_START)
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
    }

    private fun start() {
        val consent = VpnService.prepare(this)
        if (consent != null) {
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
