package com.laserpower.rtether

import android.os.ParcelFileDescriptor
import android.util.Log
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.net.Socket
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlin.concurrent.thread

/**
 * Copies raw IPv4 packets between the VPN tunnel and the upstream relay socket.
 *
 * Wire format in both directions: a big-endian uint16 length followed by one packet.
 * Length 0 is a keepalive and is ignored. Packets coming back whose destination is not
 * [ownAddress] belong to a teammate and are handed to [hub]; the hub pushes teammates'
 * packets upstream through [sendUpstream].
 *
 * [connect] must return a socket that is connected, protected from the VPN, and past any
 * handshake, so the same class serves the USB link and the hub-client link.
 */
class Tunnel(
    private val fd: ParcelFileDescriptor,
    private val ownAddress: Int,
    private val connect: () -> Socket,
) {
    companion object {
        const val STATE_STOPPED = "Stopped"
        const val STATE_CONNECTING = "Connecting"
        const val STATE_CONNECTED = "Connected"
        private const val MAX_FRAME = 1500
        private const val TAG = "rtether"
        private val RECONNECT_DELAYS_MS = longArrayOf(1000, 2000, 4000, 5000)
    }

    private val _state = MutableStateFlow(STATE_STOPPED)
    val state: StateFlow<String> = _state

    @Volatile var hub: HubServer? = null

    @Volatile private var running = false
    @Volatile private var socket: Socket? = null
    @Volatile private var socketOut: OutputStream? = null
    private var connectThread: Thread? = null
    private var tunReader: Thread? = null

    fun start() {
        if (running) return
        running = true
        tunReader = thread(name = "tun-to-sock") { readTunnelForever() }
        connectThread = thread(name = "sock-to-tun") { connectLoop() }
    }

    fun stop() {
        running = false
        closeSocket()
        connectThread?.interrupt()
        tunReader?.interrupt()
        _state.value = STATE_STOPPED
    }

    /** Frames one packet to the relay. Returns false (packet dropped) when no relay is connected. */
    fun sendUpstream(packet: ByteArray, length: Int): Boolean {
        val out = socketOut ?: return false
        try {
            synchronized(out) {
                out.write(length shr 8)
                out.write(length and 0xff)
                out.write(packet, 0, length)
                out.flush()
            }
        } catch (e: IOException) {
            Log.d(TAG, "socket write failed: ${e.message}")
            closeSocket()
            return false
        }
        return true
    }

    /** One persistent reader on the tunnel: frames each of this phone's packets upstream. */
    private fun readTunnelForever() {
        val input = FileInputStream(fd.fileDescriptor)
        val buffer = ByteArray(MAX_FRAME)
        while (running) {
            val n = try {
                input.read(buffer)
            } catch (e: IOException) {
                Log.d(TAG, "tunnel read ended: ${e.message}")
                return
            }
            if (n <= 0) continue
            sendUpstream(buffer, n)
        }
    }

    private fun connectLoop() {
        var attempt = 0
        while (running) {
            _state.value = STATE_CONNECTING
            try {
                val s = connect()
                socket = s
                socketOut = BufferedOutputStream(s.getOutputStream(), 65536)
                attempt = 0
                _state.value = STATE_CONNECTED
                Log.i(TAG, "connected upstream")
                readSocket(s)
            } catch (e: IOException) {
                Log.d(TAG, "upstream connection failed: ${e.message}")
            }
            closeSocket()
            if (!running) break
            _state.value = STATE_CONNECTING
            val delay = RECONNECT_DELAYS_MS[minOf(attempt, RECONNECT_DELAYS_MS.size - 1)]
            attempt++
            try {
                Thread.sleep(delay)
            } catch (e: InterruptedException) {
                break
            }
        }
        _state.value = STATE_STOPPED
    }

    /** Reads frames from the relay and routes each packet to this phone's tunnel or a teammate. */
    private fun readSocket(s: Socket) {
        val input = DataInputStream(BufferedInputStream(s.getInputStream(), 65536))
        val output = FileOutputStream(fd.fileDescriptor)
        val buffer = ByteArray(MAX_FRAME)
        while (running) {
            val length = input.readUnsignedShort()
            if (length == 0) continue // keepalive
            if (length > MAX_FRAME) throw IOException("frame of $length bytes exceeds $MAX_FRAME")
            input.readFully(buffer, 0, length)
            val destination = HubProtocol.ipv4Destination(buffer, length)
            if (destination == ownAddress) {
                output.write(buffer, 0, length)
            } else {
                hub?.deliver(buffer, length) // dropped when no hub or unknown teammate
            }
        }
    }

    private fun closeSocket() {
        val s = socket ?: return
        socket = null
        socketOut = null
        try {
            s.close()
        } catch (e: IOException) {
            // already closed
        }
    }
}
