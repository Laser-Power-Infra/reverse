package com.laserpower.rtether

import android.system.Os
import android.system.OsConstants
import android.system.StructTimeval
import android.util.Log
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileDescriptor
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import kotlin.concurrent.thread

/** Pure HTTP bits of the APK download server; unit-tested on the JVM. */
object ApkServerProtocol {
    const val APK_PATH = "/rtether.apk"

    /** The path of a `GET <path> HTTP/1.x` request line, or null for anything else. */
    fun requestPath(requestLine: String): String? {
        val parts = requestLine.trim().split(" ")
        if (parts.size != 3 || parts[0] != "GET" || !parts[2].startsWith("HTTP/")) return null
        return parts[1]
    }

    fun responseHeader(
        status: Int,
        reason: String,
        contentType: String,
        length: Long,
        attachment: String? = null,
    ): ByteArray {
        val lines = StringBuilder()
            .append("HTTP/1.0 ").append(status).append(' ').append(reason).append("\r\n")
            .append("Content-Type: ").append(contentType).append("\r\n")
            .append("Content-Length: ").append(length).append("\r\n")
            .append("Connection: close\r\n")
        if (attachment != null) {
            lines.append("Content-Disposition: attachment; filename=\"").append(attachment).append("\"\r\n")
        }
        return lines.append("\r\n").toString().toByteArray()
    }

    fun landingPage(): String =
        "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width\">" +
            "<title>Reverse Tether</title></head><body style=\"font-family:sans-serif;padding:24px\">" +
            "<h2>Reverse Tether</h2><p><a href=\"$APK_PATH\" style=\"font-size:20px\">Download the app</a></p>" +
            "<p>After installing: open Reverse Tether, enter this phone's address, tap Connect.</p>" +
            "</body></html>"
}

/** Serves this app's own APK over the hotspot so teammates can install it from a browser. */
class ApkServer(
    private val port: Int,
    protectFd: (Int) -> Boolean,
    private val apkPath: String,
) {
    companion object {
        private const val TAG = "rtether"
        private const val REQUEST_TIMEOUT_MS = 10_000L
    }

    private val listener = ProtectedListener(port, protectFd)
    @Volatile private var running = false

    fun start() {
        if (running) return
        running = true
        listener.start()
        thread(name = "apk-accept") {
            while (running) {
                val client = listener.accept() ?: break
                thread(name = "apk-client") { serve(client) }
            }
        }
        Log.i(TAG, "apk server listening on port $port")
    }

    fun stop() {
        running = false
        listener.close()
    }

    private fun serve(fd: FileDescriptor) {
        try {
            Os.setsockoptTimeval(
                fd, OsConstants.SOL_SOCKET, OsConstants.SO_RCVTIMEO,
                StructTimeval.fromMillis(REQUEST_TIMEOUT_MS),
            )
            val input = FileInputStream(fd)
            val output = BufferedOutputStream(FileOutputStream(fd), 65536)
            val requestLine = readLine(input)
            while (true) {
                val header = readLine(input) // skip request headers
                if (header.isEmpty()) break
            }
            when (ApkServerProtocol.requestPath(requestLine)) {
                "/" -> {
                    val body = ApkServerProtocol.landingPage().toByteArray()
                    output.write(ApkServerProtocol.responseHeader(200, "OK", "text/html; charset=utf-8", body.size.toLong()))
                    output.write(body)
                }
                ApkServerProtocol.APK_PATH -> {
                    val file = File(apkPath)
                    output.write(
                        ApkServerProtocol.responseHeader(
                            200, "OK", "application/vnd.android.package-archive", file.length(), "rtether.apk",
                        ),
                    )
                    file.inputStream().use { it.copyTo(output) }
                }
                else -> {
                    val body = "not found".toByteArray()
                    output.write(ApkServerProtocol.responseHeader(404, "Not Found", "text/plain", body.size.toLong()))
                    output.write(body)
                }
            }
            output.flush()
        } catch (e: Exception) {
            Log.d(TAG, "apk request failed: ${e.message}")
        } finally {
            ProtectedListener.closeQuietly(fd)
        }
    }

    private fun readLine(input: FileInputStream): String {
        val line = StringBuilder()
        while (line.length < 4096) {
            val b = input.read()
            if (b < 0) throw IOException("connection closed")
            if (b == '\n'.code) break
            if (b != '\r'.code) line.append(b.toChar())
        }
        return line.toString()
    }
}
