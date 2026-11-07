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
import java.net.InetSocketAddress
import java.net.Socket
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlin.concurrent.thread

/**
 * Copies raw IPv4 packets between the VPN tunnel and the relay on the PC.
 *
 * Wire format in both directions: a big-endian uint16 length followed by one packet.
 * Length 0 is a keepalive and is ignored. Nothing here inspects the packets.
 */
class Tunnel(
    private val fd: ParcelFileDescriptor,
    private val protect: (Socket) -> Boolean,
    private val host: String = "127.0.0.1",
    private val port: Int = 31416,
) {
    companion object {
        const val STATE_STOPPED = "Stopped"
        const val STATE_CONNECTING = "Connecting to PC"
        const val STATE_CONNECTED = "Connected"
        private const val MAX_FRAME = 1500
        private const val TAG = "rtether"
        private val RECONNECT_DELAYS_MS = longArrayOf(1000, 2000, 4000, 5000)
    }

    private val _state = MutableStateFlow(STATE_STOPPED)
    val state: StateFlow<String> = _state

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

    /** One persistent reader on the tunnel: frames each packet to whichever socket is live. */
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
            val out = socketOut ?: continue // no relay connected: drop the packet
            try {
                synchronized(out) {
                    out.write(n shr 8)
                    out.write(n and 0xff)
                    out.write(buffer, 0, n)
                    out.flush()
                }
            } catch (e: IOException) {
                Log.d(TAG, "socket write failed: ${e.message}")
                closeSocket()
            }
        }
    }

    private fun connectLoop() {
        var attempt = 0
        while (running) {
            _state.value = STATE_CONNECTING
            try {
                val s = Socket()
                protect(s)
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(host, port), 3000)
                socket = s
                socketOut = BufferedOutputStream(s.getOutputStream(), 65536)
                attempt = 0
                _state.value = STATE_CONNECTED
                Log.i(TAG, "connected to relay")
                readSocket(s)
            } catch (e: IOException) {
                Log.d(TAG, "relay connection failed: ${e.message}")
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

    /** Reads frames from the relay and writes each packet into the tunnel. */
    private fun readSocket(s: Socket) {
        val input = DataInputStream(BufferedInputStream(s.getInputStream(), 65536))
        val output = FileOutputStream(fd.fileDescriptor)
        val buffer = ByteArray(MAX_FRAME)
        while (running) {
            val length = input.readUnsignedShort()
            if (length == 0) continue // keepalive
            if (length > MAX_FRAME) throw IOException("frame of $length bytes exceeds $MAX_FRAME")
            input.readFully(buffer, 0, length)
            output.write(buffer, 0, length)
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
