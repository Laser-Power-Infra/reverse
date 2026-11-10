package com.laserpower.rtether

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Test

class ManifestTest {
    private val manifest = File("src/main/AndroidManifest.xml").readText()

    @Test
    fun `client mode may call requestNetwork`() {
        // requestNetwork throws SecurityException without this normal-level permission.
        assertTrue(manifest.contains("android.permission.CHANGE_NETWORK_STATE"))
    }
}
