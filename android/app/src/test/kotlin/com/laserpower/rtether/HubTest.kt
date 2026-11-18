package com.laserpower.rtether

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class HubTest {

    private val first = HubProtocol.ip(10, 8, 0, 10)
    private val second = HubProtocol.ip(10, 8, 0, 11)

    @Test
    fun `pool assigns the first address when nothing is wanted`() {
        val pool = AddressPool()
        assertEquals(first, pool.assign(0))
        assertEquals(second, pool.assign(0))
    }

    @Test
    fun `wanted free address is honoured`() {
        val pool = AddressPool()
        val wanted = HubProtocol.ip(10, 8, 0, 42)
        assertEquals(wanted, pool.assign(wanted))
    }

    @Test
    fun `wanted address in use yields another`() {
        val pool = AddressPool()
        assertEquals(first, pool.assign(first))
        assertEquals(second, pool.assign(first))
    }

    @Test
    fun `wanted address outside the pool is ignored`() {
        val pool = AddressPool()
        assertEquals(first, pool.assign(HubProtocol.ip(10, 8, 0, 2)))
        assertEquals(second, pool.assign(HubProtocol.ip(192, 168, 1, 5)))
    }

    @Test
    fun `release makes address reusable`() {
        val pool = AddressPool()
        assertEquals(first, pool.assign(0))
        pool.release(first)
        assertEquals(first, pool.assign(0))
    }

    @Test
    fun `full pool returns zero`() {
        val pool = AddressPool(first = 10, last = 12)
        assertEquals(HubProtocol.ip(10, 8, 0, 10), pool.assign(0))
        assertEquals(HubProtocol.ip(10, 8, 0, 11), pool.assign(0))
        assertEquals(HubProtocol.ip(10, 8, 0, 12), pool.assign(0))
        assertEquals(0, pool.assign(0))
    }

    @Test
    fun `hello round trip`() {
        val wanted = HubProtocol.ip(10, 8, 0, 77)
        val frame = HubProtocol.encodeHello(wanted)
        assertEquals(8, frame.size)
        assertEquals(wanted, HubProtocol.decodeHello(frame))
        assertEquals(0, HubProtocol.decodeHello(HubProtocol.encodeHello(0)))
    }

    @Test
    fun `malformed hello is rejected`() {
        assertNull(HubProtocol.decodeHello(byteArrayOf(1, 2, 3)))
        assertNull(HubProtocol.decodeHello("XXXX\u0000\u0000\u0000\u0000".toByteArray()))
        assertNull(HubProtocol.decodeHello(ByteArray(20)))
    }

    @Test
    fun `reply round trip and full reply`() {
        assertEquals(first, HubProtocol.decodeReply(HubProtocol.encodeReply(first)))
        assertArrayEquals(ByteArray(4), HubProtocol.encodeReply(0))
        assertNull(HubProtocol.decodeReply(ByteArray(3)))
    }

    @Test
    fun `source and destination are read from the IPv4 header`() {
        val packet = ByteArray(40)
        packet[0] = 0x45
        packet[12] = 10; packet[13] = 8; packet[14] = 0; packet[15] = 10
        packet[16] = 1; packet[17] = 1; packet[18] = 1; packet[19] = 1
        assertEquals(first, HubProtocol.sourceKey(packet, packet.size))
        assertEquals(HubProtocol.ip(1, 1, 1, 1), HubProtocol.destinationKey(packet, packet.size))
        assertEquals(0, HubProtocol.sourceKey(packet, 10))
    }

    @Test
    fun `IPv6 tunnel addresses map to the phone's IPv4 key`() {
        val packet = ByteArray(60)
        packet[0] = 0x60
        packet[8] = 0xfd.toByte(); packet[11] = 8; packet[23] = 10 // source fd00:8::a
        packet[24] = 0x20; packet[25] = 0x01; packet[39] = 1 // destination 2001::1
        assertEquals(first, HubProtocol.sourceKey(packet, packet.size))
        assertEquals(0, HubProtocol.destinationKey(packet, packet.size))
        assertEquals(0, HubProtocol.sourceKey(packet, 39))
        assertEquals("fd00:8::a", HubProtocol.dottedV6(first))
    }

    @Test
    fun `dotted formatting`() {
        assertEquals("10.8.0.10", HubProtocol.dotted(first))
        assertEquals("192.168.1.5", HubProtocol.dotted(HubProtocol.ip(192, 168, 1, 5)))
    }

    @Test
    fun `constants match the spec`() {
        assertEquals(31417, HubProtocol.HUB_PORT)
        assertEquals(8, HubProtocol.MAX_CLIENTS)
        assertEquals(10, HubProtocol.POOL_FIRST)
        assertEquals(250, HubProtocol.POOL_LAST)
    }
}
