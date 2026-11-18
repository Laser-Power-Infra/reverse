package com.laserpower.rtether

/**
 * Pure logic for hub mode: the hello handshake, address pool, and IPv4 header peeks.
 * No Android dependencies so it is unit-tested on the JVM.
 */
object HubProtocol {
    const val HUB_PORT = 31417
    const val APK_PORT = 31418
    const val MAX_CLIENTS = 8
    const val POOL_FIRST = 10
    const val POOL_LAST = 250
    const val KEEPALIVE_MS = 10_000L

    /** 10.8.0.0, the tunnel subnet shared by the host (10.8.0.2) and all teammates. */
    val SUBNET: Int = ip(10, 8, 0, 0)

    private val HELLO_MAGIC = byteArrayOf('R'.code.toByte(), 'T'.code.toByte(), 'H'.code.toByte(), '1'.code.toByte())

    fun ip(a: Int, b: Int, c: Int, d: Int): Int =
        ((a and 0xff) shl 24) or ((b and 0xff) shl 16) or ((c and 0xff) shl 8) or (d and 0xff)

    fun dotted(ip: Int): String =
        "${ip ushr 24 and 0xff}.${ip ushr 16 and 0xff}.${ip ushr 8 and 0xff}.${ip and 0xff}"

    fun encodeHello(wanted: Int): ByteArray = HELLO_MAGIC + toBytes(wanted)

    /** Returns the wanted address (0 for none), or null when the frame is not a hello. */
    fun decodeHello(frame: ByteArray): Int? {
        if (frame.size != 8) return null
        for (i in 0 until 4) if (frame[i] != HELLO_MAGIC[i]) return null
        return readInt(frame, 4)
    }

    fun encodeReply(ip: Int): ByteArray = toBytes(ip)

    fun decodeReply(frame: ByteArray): Int? = if (frame.size == 4) readInt(frame, 0) else null

    /** fd00:8::X, the IPv6 address of the phone whose IPv4 address is 10.8.0.X. */
    fun dottedV6(ip: Int): String = "fd00:8::${(ip and 0xff).toString(16)}"

    /** The 10.8.0.X address of the phone a packet came from; 0 when it is not a tunnel address. */
    fun sourceKey(packet: ByteArray, length: Int): Int = addressKey(packet, length, 12, 8)

    /** The 10.8.0.X address of the phone a packet is for; 0 when it is not a tunnel address. */
    fun destinationKey(packet: ByteArray, length: Int): Int = addressKey(packet, length, 16, 24)

    private fun addressKey(packet: ByteArray, length: Int, v4At: Int, v6At: Int): Int {
        if (length < 20) return 0
        if ((packet[0].toInt() and 0xf0) != 0x60) return readInt(packet, v4At)
        if (length < 40) return 0
        for (i in V6_PREFIX.indices) if (packet[v6At + i] != V6_PREFIX[i]) return 0
        return SUBNET or (packet[v6At + 15].toInt() and 0xff)
    }

    /** The first 15 bytes of fd00:8::X. */
    private val V6_PREFIX = byteArrayOf(0xfd.toByte(), 0, 0, 8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    private fun toBytes(v: Int) = byteArrayOf(
        (v ushr 24).toByte(), (v ushr 16).toByte(), (v ushr 8).toByte(), v.toByte(),
    )

    private fun readInt(b: ByteArray, at: Int): Int =
        ((b[at].toInt() and 0xff) shl 24) or ((b[at + 1].toInt() and 0xff) shl 16) or
            ((b[at + 2].toInt() and 0xff) shl 8) or (b[at + 3].toInt() and 0xff)
}

/** Hands out 10.8.0.[first..last] addresses to teammates; thread-safe. */
class AddressPool(
    private val first: Int = HubProtocol.POOL_FIRST,
    private val last: Int = HubProtocol.POOL_LAST,
) {
    private val inUse = HashSet<Int>()

    /** Returns the wanted address if it is in the pool and free, else the next free one, else 0. */
    @Synchronized
    fun assign(wanted: Int): Int {
        if (wanted != 0 && inPool(wanted) && wanted !in inUse) {
            inUse += wanted
            return wanted
        }
        for (host in first..last) {
            val candidate = HubProtocol.SUBNET or host
            if (candidate !in inUse) {
                inUse += candidate
                return candidate
            }
        }
        return 0
    }

    @Synchronized
    fun release(ip: Int) {
        inUse -= ip
    }

    @Synchronized
    fun size(): Int = inUse.size

    private fun inPool(ip: Int): Boolean =
        (ip and 0xffffff00.toInt()) == HubProtocol.SUBNET && (ip and 0xff) in first..last
}
