package com.laserpower.rtether

import android.Manifest
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.VpnService
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Button
import android.widget.CompoundButton
import android.widget.EditText
import android.widget.ImageView
import android.widget.Switch
import com.google.zxing.BarcodeFormat
import com.google.zxing.qrcode.QRCodeWriter
import java.net.Inet4Address
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

/**
 * One screen: Start/Stop for the USB link, a switch to share with teammates over the
 * hotspot, and a field + button to connect to a teammate's hub instead.
 * Also the entry point the PC relay uses over adb:
 * `am start -n com.laserpower.rtether/.MainActivity -a com.laserpower.rtether.START`.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var status: TextView
    private lateinit var toggle: Button
    private lateinit var shareSwitch: Switch
    private lateinit var hubInfo: TextView
    private lateinit var hubQr: ImageView
    private lateinit var hubQrCaption: TextView
    private var qrLink = ""
    private var autoFilledHost = ""
    private lateinit var hubHost: EditText
    private lateinit var connectHub: Button
    private var finishAfterConsent = false
    private var pendingAction: String = TetherVpnService.ACTION_START
    private var pendingHubHost: String = ""

    private val vpnConsent =
        registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
            if (result.resultCode == RESULT_OK) sendToService(pendingAction, pendingHubHost)
            if (finishAfterConsent) finish()
        }

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    private val shareListener = CompoundButton.OnCheckedChangeListener { _, checked ->
        sendToService(
            if (checked) TetherVpnService.ACTION_HUB_ON else TetherVpnService.ACTION_HUB_OFF,
        )
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        toggle = findViewById(R.id.toggle)
        shareSwitch = findViewById(R.id.share_switch)
        hubInfo = findViewById(R.id.hub_info)
        hubQr = findViewById(R.id.hub_qr)
        hubQrCaption = findViewById(R.id.hub_qr_caption)
        hubHost = findViewById(R.id.hub_host)
        connectHub = findViewById(R.id.connect_hub)

        toggle.setOnClickListener {
            if (TetherVpnService.state.value == Tunnel.STATE_STOPPED) {
                startWithConsent(TetherVpnService.ACTION_START)
            } else {
                sendToService(TetherVpnService.ACTION_STOP)
            }
        }
        connectHub.setOnClickListener {
            val host = hubHost.text.toString().trim().substringBefore(":")
            if (host.isNotEmpty()) startWithConsent(TetherVpnService.ACTION_START_CLIENT, host)
        }
        shareSwitch.setOnCheckedChangeListener(shareListener)

        lifecycleScope.launch {
            TetherVpnService.state.collect { value ->
                status.text = value
                toggle.text = getString(
                    if (value == Tunnel.STATE_STOPPED) R.string.start else R.string.stop,
                )
                refreshHubControls()
            }
        }
        lifecycleScope.launch { TetherVpnService.mode.collect { refreshHubControls() } }
        lifecycleScope.launch { TetherVpnService.hubAddress.collect { refreshHubControls() } }
        lifecycleScope.launch { TetherVpnService.hubClients.collect { refreshHubControls() } }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        handleRelayIntent(intent)
    }

    /**
     * On a teammate's hotspot the hub phone is the Wi-Fi gateway, so its address can be
     * filled in for them. A callback (not a one-off read) so it also fills when Wi-Fi
     * joins after the screen is already open.
     */
    private val wifiCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onLinkPropertiesChanged(network: Network, link: LinkProperties) {
            val gateway = link.routes
                .firstOrNull { it.isDefaultRoute && it.gateway is Inet4Address }?.gateway
                ?: return
            val address = "${gateway.hostAddress}:${HubProtocol.HUB_PORT}"
            runOnUiThread {
                // Never overwrite what the user typed; do refresh our own earlier fill.
                val current = hubHost.text.toString()
                if (current.isEmpty() || current == autoFilledHost) {
                    hubHost.setText(address)
                    autoFilledHost = address
                }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        val wifi = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        getSystemService(ConnectivityManager::class.java).registerNetworkCallback(wifi, wifiCallback)
    }

    override fun onStop() {
        getSystemService(ConnectivityManager::class.java).unregisterNetworkCallback(wifiCallback)
        super.onStop()
    }

    private fun qrBitmap(text: String): Bitmap {
        val size = 512
        val matrix = QRCodeWriter().encode(text, BarcodeFormat.QR_CODE, size, size)
        val pixels = IntArray(size * size) {
            if (matrix.get(it % size, it / size)) Color.BLACK else Color.WHITE
        }
        return Bitmap.createBitmap(pixels, size, size, Bitmap.Config.RGB_565)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleRelayIntent(intent)
    }

    private fun refreshHubControls() {
        val usbRunning = TetherVpnService.mode.value == TetherVpnService.MODE_USB &&
            TetherVpnService.state.value != Tunnel.STATE_STOPPED
        val address = TetherVpnService.hubAddress.value
        val sharing = address.isNotEmpty()
        shareSwitch.isEnabled = usbRunning
        shareSwitch.setOnCheckedChangeListener(null)
        shareSwitch.isChecked = sharing
        shareSwitch.setOnCheckedChangeListener(shareListener)
        hubInfo.text = when {
            !usbRunning -> getString(R.string.hub_needs_usb)
            !sharing -> getString(R.string.hub_off)
            address == "?" -> getString(R.string.hub_no_hotspot)
            else -> getString(
                R.string.hub_on,
                address,
                TetherVpnService.hubClients.value,
                address.substringBefore(":"),
            )
        }
        val link = if (usbRunning && sharing && address != "?") {
            "http://${address.substringBefore(":")}:${HubProtocol.APK_PORT}"
        } else {
            ""
        }
        if (link != qrLink) { // this runs every few seconds; only redraw on change
            qrLink = link
            if (link.isNotEmpty()) hubQr.setImageBitmap(qrBitmap(link))
            val visibility = if (link.isEmpty()) View.GONE else View.VISIBLE
            hubQr.visibility = visibility
            hubQrCaption.visibility = visibility
        }
        val clientRunning =TetherVpnService.mode.value == TetherVpnService.MODE_CLIENT
        connectHub.isEnabled = !usbRunning && !clientRunning
    }

    /** Forwards START/STOP sent by the relay over adb; closes again unless consent is needed. */
    private fun handleRelayIntent(intent: Intent?) {
        when (intent?.action) {
            TetherVpnService.ACTION_START -> {
                val consent = VpnService.prepare(this)
                if (consent != null) {
                    finishAfterConsent = true
                    pendingAction = TetherVpnService.ACTION_START
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
            TetherVpnService.ACTION_HUB_ON, TetherVpnService.ACTION_HUB_OFF -> {
                sendToService(intent.action!!)
                finish()
            }
        }
    }

    private fun startWithConsent(action: String, host: String = "") {
        pendingAction = action
        pendingHubHost = host
        finishAfterConsent = false
        val consent = VpnService.prepare(this)
        if (consent != null) vpnConsent.launch(consent) else sendToService(action, host)
    }

    private fun sendToService(action: String, host: String = "") {
        val intent = Intent(this, TetherVpnService::class.java).setAction(action)
        if (host.isNotEmpty()) intent.putExtra(TetherVpnService.EXTRA_HUB_HOST, host)
        ContextCompat.startForegroundService(this, intent)
    }
}
