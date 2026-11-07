package com.laserpower.rtether

import android.os.ParcelFileDescriptor
import android.system.ErrnoException
import android.system.Os
import android.system.OsConstants
import android.util.Log
import java.io.FileDescriptor
import java.net.InetAddress

/**
 * A TCP listener whose sockets are protected from the VPN.
 *
 * On this phone every unprotected app socket is routed into the tunnel; protected ones use
 * the normal local-network routes and so can talk to hotspot clients on ap0. Java's
 * ServerSocket cannot be protected, so the listener is built from raw descriptors via [Os].
 */
class ProtectedListener(
    private val port: Int,
    private val protectFd: (Int) -> Boolean,
    private val backlog: Int = 8,
) {
    companion object {
        private const val TAG = "rtether"

        fun protect(fd: FileDescriptor, protectFd: (Int) -> Boolean) {
            ParcelFileDescriptor.dup(fd).use { dup ->
                if (!protectFd(dup.fd)) Log.w(TAG, "could not protect socket")
            }
        }

        fun closeQuietly(fd: FileDescriptor) {
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

    @Volatile private var fd: FileDescriptor? = null

    fun start() {
        val socket = Os.socket(OsConstants.AF_INET, OsConstants.SOCK_STREAM, 0)
        protect(socket, protectFd)
        Os.setsockoptInt(socket, OsConstants.SOL_SOCKET, OsConstants.SO_REUSEADDR, 1)
        Os.bind(socket, InetAddress.getByName("0.0.0.0"), port)
        Os.listen(socket, backlog)
        fd = socket
    }

    /** Blocks for the next client; returns null once the listener is closed. */
    fun accept(): FileDescriptor? {
        val listening = fd ?: return null
        val client = try {
            Os.accept(listening, null)
        } catch (e: ErrnoException) {
            if (fd != null) Log.d(TAG, "accept on port $port ended: ${e.message}")
            return null
        }
        protect(client, protectFd)
        return client
    }

    fun close() {
        val listening = fd ?: return
        fd = null
        closeQuietly(listening)
    }
}
