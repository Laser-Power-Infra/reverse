package com.laserpower.rtether

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import android.util.Log
import androidx.core.app.NotificationCompat
import java.io.DataInputStream
import java.io.IOException
import java.net.Inet4Address
import java.net.InetSocketAddress
import java.net.NetworkInterface
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlin.concurrent.thread

/**
 * Owns the VPN interface and a [Tunnel] to the upstream: the PC relay over USB, or a
 * teammate's phone running as a hub. In USB mode it can also run a [HubServer].
 */
class TetherVpnService : VpnService() {

    companion object {
        const val ACTION_START = "com.laserpower.rtether.START"
        const val ACTION_START_CLIENT = "com.laserpower.rtether.START_CLIENT"
        const val ACTION_HUB_ON = "com.laserpower.rtether.HUB_ON"
        const val ACTION_HUB_OFF = "com.laserpower.rtether.HUB_OFF"
        const val ACTION_STOP = "com.laserpower.rtether.STOP"
        const val EXTRA_HUB_HOST = "hub_host"

        const val MODE_NONE = "none"
        const val MODE_USB = "usb"
        const val MODE_CLIENT = "client"

        private const val CHANNEL_ID = "rtether"
        private const val NOTIFICATION_ID = 1
        private const val TAG = "rtether"
        private const val RELAY_PORT = 31416
        private const val USB_CONNECT_TIMEOUT_MS = 3000
        private const val HUB_CONNECT_TIMEOUT_MS = 5000
        private val HOST_ADDRESS = HubProtocol.ip(10, 8, 0, 2)

        private val _state = MutableStateFlow(Tunnel.STATE_STOPPED)
        val state: StateFlow<String> = _state
        private val _mode = MutableStateFlow(MODE_NONE)
        val mode: StateFlow<String> = _mode
        private val _hubAddress = MutableStateFlow("")
        /** "ip:port" teammates must type, "" when the hub is off, "?" when the hotspot is off. */
        val hubAddress: StateFlow<String> = _hubAddress
        private val _hubClients = MutableStateFlow(0)
        val hubClients: StateFlow<Int> = _hubClients

        /** First IPv4 address of a hotspot-looking interface, or null when the hotspot is off. */
        fun hotspotAddress(): String? {
            val interfaces = NetworkInterface.getNetworkInterfaces() ?: return null
            for (iface in interfaces) {
                val name = iface.name
                val looksLikeHotspot = name.startsWith("ap") || name.startsWith("swlan") ||
                    name.startsWith("softap") || name == "wlan1"
                if (!looksLikeHotspot) continue
                for (address in iface.inetAddresses) {
                    if (address is Inet4Address) return address.hostAddress
                }
            }
            return null
        }
    }

    private var tunnel: Tunnel? = null
    private var tunFd: ParcelFileDescriptor? = null
    private var hub: HubServer? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var stateJob: Job? = null
    private var hubJob: Job? = null
    @Volatile private var clientAddress = 0

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopTunnel()
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_START_CLIENT -> startClient(intent.getStringExtra(EXTRA_HUB_HOST) ?: "")
            ACTION_HUB_ON -> startHub()
            ACTION_HUB_OFF -> stopHub()
            else -> startUsb()
        }
        return START_STICKY
    }

    // -- USB mode -------------------------------------------------------------

    private fun startUsb() {
        if (tunnel != null) return
        startInForeground()
        val fd = establish(HOST_ADDRESS) ?: run {
            stopTunnel()
            stopSelf()
            return
        }
        _mode.value = MODE_USB
        runTunnel(fd, HOST_ADDRESS) {
            Socket().also {
                protect(it)
                it.tcpNoDelay = true
                it.connect(InetSocketAddress("127.0.0.1", RELAY_PORT), USB_CONNECT_TIMEOUT_MS)
            }
        }
    }

    // -- Client mode (teammate phone) ----------------------------------------------

    private fun startClient(hubHost: String) {
        if (tunnel != null || hubHost.isBlank()) return
        startInForeground()
        _mode.value = MODE_CLIENT
        _state.value = Tunnel.STATE_CONNECTING
        thread(name = "hub-client-start") {
            try {
                val wifi = awaitWifiNetwork() ?: throw IOException("join the hub phone's hotspot first")
                val first = connectToHub(wifi, hubHost, wanted = 0)
                val fd = establish(clientAddress) ?: throw IOException("VPN permission not granted")
                runTunnel(fd, clientAddress, initial = first) {
                    connectToHub(wifi, hubHost, wanted = clientAddress)
                }
            } catch (e: IOException) {
                Log.w(TAG, "client start failed: ${e.message}")
                _state.value = "Failed: ${e.message}"
                scope.launch {
                    delay(4000)
                    stopTunnel()
                    stopSelf()
                }
            }
        }
    }

    private fun awaitWifiNetwork(): Network? {
        val cm = getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val latch = CountDownLatch(1)
        var found: Network? = null
        val request = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                found = network
                latch.countDown()
            }
        }
        cm.requestNetwork(request, callback)
        latch.await(10, TimeUnit.SECONDS)
        return found
    }

    /** Connects to the hub over Wi-Fi, performs the hello, and records the assigned address. */
    private fun connectToHub(wifi: Network, hubHost: String, wanted: Int): Socket {
        val socket = Socket()
        protect(socket)
        wifi.bindSocket(socket)
        socket.tcpNoDelay = true
        socket.connect(InetSocketAddress(hubHost, HubProtocol.HUB_PORT), HUB_CONNECT_TIMEOUT_MS)
        val hello = HubProtocol.encodeHello(wanted)
        val out = socket.getOutputStream()
        out.write(0)
        out.write(hello.size)
        out.write(hello)
        out.flush()
        val input = DataInputStream(socket.getInputStream())
        val length = input.readUnsignedShort()
        val reply = ByteArray(length)
        input.readFully(reply)
        val assigned = HubProtocol.decodeReply(reply) ?: 0
        if (assigned == 0) {
            socket.close()
            throw IOException("hub is full")
        }
        if (wanted != 0 && assigned != wanted) {
            socket.close()
            throw IOException("hub gave a different address; retrying")
        }
        clientAddress = assigned
        return socket
    }

    // -- Hub (host phone sharing with teammates) -----------------------------------

    private fun startHub() {
        val t = tunnel ?: return
        if (hub != null || _mode.value != MODE_USB) return
        val server = HubServer(protectFd = { protect(it) }, upstream = t::sendUpstream)
        try {
            server.start()
        } catch (e: Exception) {
            Log.e(TAG, "hub start failed: ${e.message}")
            return
        }
        hub = server
        t.hub = server
        hubJob = scope.launch {
            launch { server.clientCount.collect { _hubClients.value = it } }
            while (isActive) {
                val address = hotspotAddress()
                _hubAddress.value = if (address == null) "?" else "$address:${HubProtocol.HUB_PORT}"
                delay(3000)
            }
        }
    }

    private fun stopHub() {
        hubJob?.cancel()
        hubJob = null
        tunnel?.hub = null
        hub?.stop()
        hub = null
        _hubAddress.value = ""
        _hubClients.value = 0
    }

    // -- shared ----------------------------------------------------------------

    private fun establish(address: Int): ParcelFileDescriptor? {
        val fd = try {
            Builder()
                .setSession(getString(R.string.app_name))
                .addAddress(HubProtocol.dotted(address), 24)
                .addRoute("0.0.0.0", 0)
                .addDnsServer("10.8.0.1")
                .setMtu(1400)
                .setBlocking(true)
                .establish()
        } catch (e: Exception) {
            Log.e(TAG, "establish failed: ${e.message}")
            null
        }
        if (fd == null) Log.w(TAG, "VPN permission not granted yet; open the app and press Start")
        return fd
    }

    private fun runTunnel(
        fd: ParcelFileDescriptor,
        ownAddress: Int,
        initial: Socket? = null,
        connect: () -> Socket,
    ) {
        tunFd = fd
        var pending = initial
        val t = Tunnel(fd, ownAddress) {
            val ready = pending
            pending = null
            ready ?: connect()
        }
        tunnel = t
        stateJob = scope.launch { t.state.collect { _state.value = it } }
        t.start()
    }

    private fun stopTunnel() {
        stopHub()
        stateJob?.cancel()
        stateJob = null
        tunnel?.stop()
        tunnel = null
        try {
            tunFd?.close()
        } catch (e: Exception) {
            // already closed
        }
        tunFd = null
        _state.value = Tunnel.STATE_STOPPED
        _mode.value = MODE_NONE
        stopForeground(STOP_FOREGROUND_REMOVE)
    }

    override fun onDestroy() {
        stopTunnel()
        scope.cancel()
        super.onDestroy()
    }

    override fun onRevoke() {
        stopTunnel()
        stopSelf()
        super.onRevoke()
    }

    private fun startInForeground() {
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(
                NOTIFICATION_ID,
                notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.app_name),
                NotificationManager.IMPORTANCE_LOW,
            )
            getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
        }
        val openApp = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.notification_text))
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setContentIntent(openApp)
            .setOngoing(true)
            .build()
    }
}
