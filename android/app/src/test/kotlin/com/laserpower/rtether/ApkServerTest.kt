package com.laserpower.rtether

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ApkServerTest {

    @Test
    fun `request path is taken from a GET request line`() {
        assertEquals("/rtether.apk", ApkServerProtocol.requestPath("GET /rtether.apk HTTP/1.1"))
        assertEquals("/", ApkServerProtocol.requestPath("GET / HTTP/1.0"))
    }

    @Test
    fun `non GET or malformed request lines are rejected`() {
        assertNull(ApkServerProtocol.requestPath("POST /rtether.apk HTTP/1.1"))
        assertNull(ApkServerProtocol.requestPath("GET"))
        assertNull(ApkServerProtocol.requestPath(""))
        assertNull(ApkServerProtocol.requestPath("GET /x"))
    }

    @Test
    fun `response header carries status type length and attachment`() {
        val header = String(
            ApkServerProtocol.responseHeader(
                200, "OK", "application/vnd.android.package-archive", 12345L, "rtether.apk",
            ),
        )
        assertTrue(header.startsWith("HTTP/1.0 200 OK\r\n"))
        assertTrue(header.contains("Content-Type: application/vnd.android.package-archive\r\n"))
        assertTrue(header.contains("Content-Length: 12345\r\n"))
        assertTrue(header.contains("Content-Disposition: attachment; filename=\"rtether.apk\"\r\n"))
        assertTrue(header.endsWith("\r\n\r\n"))
    }

    @Test
    fun `response header without attachment`() {
        val header = String(ApkServerProtocol.responseHeader(404, "Not Found", "text/plain", 9L))
        assertTrue(header.startsWith("HTTP/1.0 404 Not Found\r\n"))
        assertTrue(!header.contains("Content-Disposition"))
    }

    @Test
    fun `landing page links to the apk`() {
        val page = ApkServerProtocol.landingPage()
        assertTrue(page.contains("href=\"/rtether.apk\""))
    }

    @Test
    fun `apk port constant`() {
        assertEquals(31418, HubProtocol.APK_PORT)
    }
}
