package com.laserpower.rtether

import android.util.Log
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.FileDescriptor
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlin.concurrent.thread

/**
 * Accepts teammates' phones on the hotspot and relays their packets to the upstream link.
 * Sockets come from a [ProtectedListener] so they bypass this phone's own VPN.
 */
class HubServer(
    protectFd: (Int) -> Boolean,
    private val upstream: (ByteArray, Int) -> Boolean,
) {
    companion object {
        private const val TAG = "rtether"
        private const val MAX_FRAME = 1500
    }

    private class Client(val ip: Int, val fd: FileDescriptor, val output: OutputStream)

    private val listener = ProtectedListener(HubProtocol.HUB_PORT, protectFd, HubProtocol.MAX_CLIENTS)
    private val pool = AddressPool()
    private val clients = ConcurrentHashMap<Int, Client>()
    private val _clientCount = MutableStateFlow(0)
    val clientCount: StateFlow<Int> = _clientCount

    @Volatile private var running = false
    private var keepaliveThread: Thread? = null

    fun start() {
        if (running) return
        running = true
        listener.start()
        thread(name = "hub-accept") { acceptLoop() }
        keepaliveThread = thread(name = "hub-keepalive") { keepaliveLoop() }
        Log.i(TAG, "hub listening on port ${HubProtocol.HUB_PORT}")
    }

    fun stop() {
        running = false
        listener.close()
        keepaliveThread?.interrupt()
        for (client in clients.values) ProtectedListener.closeQuietly(client.fd)
    }

    /** Writes a packet from the relay to the teammate that owns its destination address. */
    fun deliver(packet: ByteArray, length: Int): Boolean {
        val client = clients[HubProtocol.ipv4Destination(packet, length)] ?: return false
        return writeFrame(client, packet, length)
    }

    private fun acceptLoop() {
        while (running) {
            val clientFd = listener.accept() ?: break
            if (clients.size >= HubProtocol.MAX_CLIENTS) {
                ProtectedListener.closeQuietly(clientFd)
                continue
            }
            thread(name = "hub-client") { serve(clientFd) }
        }
    }

    private fun serve(fd: FileDescriptor) {
        val input = DataInputStream(BufferedInputStream(FileInputStream(fd), 65536))
        val output = BufferedOutputStream(FileOutputStream(fd), 65536)
        var ip = 0
        try {
            val helloLength = input.readUnsignedShort()
            if (helloLength != 8) throw IOException("expected a hello frame")
            val hello = ByteArray(8)
            input.readFully(hello)
            val wanted = HubProtocol.decodeHello(hello) ?: throw IOException("bad hello")
            ip = pool.assign(wanted)
            val reply = HubProtocol.encodeReply(ip)
            synchronized(output) {
                output.write(0)
                output.write(reply.size)
                output.write(reply)
                output.flush()
            }
            if (ip == 0) throw IOException("address pool full")
            val client = Client(ip, fd, output)
            clients[ip] = client
            _clientCount.value = clients.size
            Log.i(TAG, "teammate ${HubProtocol.dotted(ip)} connected")
            val buffer = ByteArray(MAX_FRAME)
            while (running) {
                val length = input.readUnsignedShort()
                if (length == 0) continue
                if (length > MAX_FRAME) throw IOException("frame of $length bytes exceeds $MAX_FRAME")
                input.readFully(buffer, 0, length)
                if (HubProtocol.ipv4Source(buffer, length) != ip) continue // forged or stray
                upstream(buffer, length)
            }
        } catch (e: IOException) {
            Log.d(TAG, "teammate ${HubProtocol.dotted(ip)} left: ${e.message}")
        } finally {
            if (ip != 0) {
                clients.remove(ip)
                pool.release(ip)
                _clientCount.value = clients.size
            }
            ProtectedListener.closeQuietly(fd)
        }
    }

    private fun keepaliveLoop() {
        while (running) {
            try {
                Thread.sleep(HubProtocol.KEEPALIVE_MS)
            } catch (e: InterruptedException) {
                return
            }
            for (client in clients.values) writeFrame(client, ByteArray(0), 0)
        }
    }

    private fun writeFrame(client: Client, packet: ByteArray, length: Int): Boolean {
        val out = client.output
        return try {
            synchronized(out) {
                out.write(length shr 8)
                out.write(length and 0xff)
                if (length > 0) out.write(packet, 0, length)
                out.flush()
            }
            true
        } catch (e: IOException) {
            ProtectedListener.closeQuietly(client.fd)
            false
        }
    }
}
