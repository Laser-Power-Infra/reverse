package com.laserpower.rtether

import android.os.ParcelFileDescriptor
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import android.util.Log
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.FileDescriptor
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.net.InetAddress
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlin.concurrent.thread

/**
 * Accepts teammates' phones on the hotspot and relays their packets to the upstream link.
 *
 * The listening socket and every accepted socket are created through [Os] so their file
 * descriptors can be protected from the VPN: on this phone every unprotected app socket is
 * routed into the tunnel, while protected ones use the normal local-network routes and so
 * reach hotspot clients on ap0.
 */
class HubServer(
    private val protectFd: (Int) -> Boolean,
    private val upstream: (ByteArray, Int) -> Boolean,
) {
    companion object {
        private const val TAG = "rtether"
        private const val MAX_FRAME = 1500
    }

    private class Client(val ip: Int, val fd: FileDescriptor, val output: OutputStream)

    private val pool = AddressPool()
    private val clients = ConcurrentHashMap<Int, Client>()
    private val _clientCount = MutableStateFlow(0)
    val clientCount: StateFlow<Int> = _clientCount

    @Volatile private var running = false
    private var listenFd: FileDescriptor? = null
    private var acceptThread: Thread? = null
    private var keepaliveThread: Thread? = null

    fun start() {
        if (running) return
        running = true
        val fd = Os.socket(OsConstants.AF_INET, OsConstants.SOCK_STREAM, 0)
        protect(fd)
        Os.setsockoptInt(fd, OsConstants.SOL_SOCKET, OsConstants.SO_REUSEADDR, 1)
        Os.bind(fd, InetAddress.getByName("0.0.0.0"), HubProtocol.HUB_PORT)
        Os.listen(fd, HubProtocol.MAX_CLIENTS)
        listenFd = fd
        acceptThread = thread(name = "hub-accept") { acceptLoop(fd) }
        keepaliveThread = thread(name = "hub-keepalive") { keepaliveLoop() }
        Log.i(TAG, "hub listening on port ${HubProtocol.HUB_PORT}")
    }

    fun stop() {
        running = false
        listenFd?.let { closeQuietly(it) }
        listenFd = null
        keepaliveThread?.interrupt()
        for (client in clients.values) closeQuietly(client.fd)
    }

    /** Writes a packet from the relay to the teammate that owns its destination address. */
    fun deliver(packet: ByteArray, length: Int): Boolean {
        val client = clients[HubProtocol.ipv4Destination(packet, length)] ?: return false
        return writeFrame(client, packet, length)
    }

    private fun protect(fd: FileDescriptor) {
        ParcelFileDescriptor.dup(fd).use { dup ->
            if (!protectFd(dup.fd)) Log.w(TAG, "could not protect hub socket")
        }
    }

    private fun acceptLoop(fd: FileDescriptor) {
        while (running) {
            val clientFd = try {
                Os.accept(fd, null)
            } catch (e: ErrnoException) {
                if (running) Log.d(TAG, "hub accept ended: ${e.message}")
                break
            }
            if (clients.size >= HubProtocol.MAX_CLIENTS) {
                closeQuietly(clientFd)
                continue
            }
            protect(clientFd)
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
            closeQuietly(fd)
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
            closeQuietly(client.fd)
            false
        }
    }

    private fun closeQuietly(fd: FileDescriptor) {
        try {
            Os.shutdown(fd, OsConstants.SHUT_RDWR)
        } catch (e: ErrnoException) {
            // not connected or already shut
        }
        try {
            Os.close(fd)
        } catch (e: ErrnoException) {
            // already closed
        }
    }
}
