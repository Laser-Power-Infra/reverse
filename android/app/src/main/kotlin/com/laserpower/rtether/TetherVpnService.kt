package com.laserpower.rtether

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** Owns the VPN interface and a [Tunnel] that pipes it to the relay on the PC. */
class TetherVpnService : VpnService() {

    companion object {
        const val ACTION_START = "com.laserpower.rtether.START"
        const val ACTION_STOP = "com.laserpower.rtether.STOP"
        private const val CHANNEL_ID = "rtether"
        private const val NOTIFICATION_ID = 1
        private const val TAG = "rtether"

        private val _state = MutableStateFlow(Tunnel.STATE_STOPPED)
        val state: StateFlow<String> = _state
    }

    private var tunnel: Tunnel? = null
    private var tunFd: ParcelFileDescriptor? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private var stateJob: Job? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        return when (intent?.action) {
            ACTION_STOP -> {
                stopTunnel()
                stopSelf()
                START_NOT_STICKY
            }
            else -> {
                startTunnel()
                START_STICKY
            }
        }
    }

    private fun startTunnel() {
        if (tunnel != null) return
        startInForeground()
        val fd = try {
            Builder()
                .setSession(getString(R.string.app_name))
                .addAddress("10.8.0.2", 24)
                .addRoute("0.0.0.0", 0)
                .addDnsServer("10.8.0.1")
                .setMtu(1400)
                .setBlocking(true)
                .establish()
        } catch (e: Exception) {
            Log.e(TAG, "establish failed: ${e.message}")
            null
        }
        if (fd == null) {
            Log.w(TAG, "VPN permission not granted yet; open the app and press Start")
            stopTunnel()
            stopSelf()
            return
        }
        tunFd = fd
        val t = Tunnel(fd, protect = { protect(it) })
        tunnel = t
        stateJob = scope.launch { t.state.collect { _state.value = it } }
        t.start()
    }

    private fun stopTunnel() {
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
