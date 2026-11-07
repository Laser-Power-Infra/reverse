# Hub mode: design spec

Date: 2026-11-07
Status: approved in conversation
Extends: `2026-11-07-reverse-tethering-design.md`

## 1. Goal

Let teammates' Android phones use the USB-tethered phone's internet by joining its Wi-Fi
hotspot. Android's own hotspot cannot forward a VPN (it only tethers from a radio or
Ethernet upstream; verified on the RMX3392 with `dumpsys tethering`), so the app itself
acts as the router.

## 2. Topology

```
teammate phone (client mode)  --Wi-Fi hotspot-->  host phone (USB mode + hub)  --USB-->  PC relay
  VpnService 10.8.0.10                                VpnService 10.8.0.2
  frames over TCP :31417                              frames over TCP :31416 (adb reverse)
```

- The host phone's hotspot comes up as a plain local network with no upstream (Android 14
  gives it a random subnet, e.g. `10.79.54.0/24` on `ap0`).
- On the host, sockets marked with `VpnService.protect()` bypass the VPN and reach hotspot
  clients through Android's local-network routing table; all other app sockets go into the
  tunnel. The hub listener and every accepted socket are therefore protected.
- The PC relay is unchanged: it keys flows by source IP, so each teammate is just another
  set of flows with source `10.8.0.N`, and replies carry that IP as destination.

## 3. Wire protocol hub <-> client (TCP port 31417)

- Same framing as the USB link: `uint16` big-endian length + one IPv4 packet; length 0 is a
  keepalive.
- First frame from the client is a hello: `b"RTH1"` followed by 4 bytes, the IPv4 address
  the client wants (all zeros if it has none yet).
- The hub replies with one 4-byte frame: the assigned IPv4 (the wanted one if free,
  otherwise the next free address in `10.8.0.10 .. 10.8.0.250`), or four zero bytes when
  no address is free, after which the hub closes the connection.
- Afterwards both directions carry raw IPv4 packets. The hub drops any client packet whose
  source is not the assigned address. The hub sends a keepalive every 10 s.
- At most 8 clients.

## 4. Android app

Package unchanged. New and changed units:

- `Hub.kt` (pure Kotlin): `AddressPool` with `assign(wanted: Int): Int` (0 when full) and
  `release(ip: Int)`; `ipv4Source(packet)`, `ipv4Destination(packet)` reading bytes 12..15
  and 16..19 as big-endian ints; `HelloCodec.encodeHello(wanted)`, `decodeHello(frame):
  Int?` (null when malformed), `encodeReply(ip)`, `decodeReply(frame): Int?`; constants
  `HUB_PORT = 31417`, `MAX_CLIENTS = 8`, `POOL_FIRST = 10`, `POOL_LAST = 250`,
  `SUBNET = 10.8.0.0`.
- `HubServer(protectFd: (Int) -> Boolean, upstream: (ByteArray, Int) -> Unit)`: listener
  via `android.system.Os`, protected through `ParcelFileDescriptor.dup(fd).fd`; per-client
  reader thread; `deliver(packet, length)` to the client owning the destination;
  keepalive thread; `clientCount: StateFlow<Int>`; `stop()`.
- `Tunnel(fd, ownAddress: Int, connect: () -> Socket)`: `sendUpstream(packet, length)`;
  packets from the relay whose destination is not `ownAddress` go to `hub?.deliver`.
- `TetherVpnService`: actions `START` (USB), `START_CLIENT` (+ extra `hub_host`),
  `HUB_ON`, `HUB_OFF`, `STOP`. State flows: `state`, `hubAddress`, `hubClients`.
  Client mode binds its socket to the Wi-Fi `Network` from `requestNetwork(TRANSPORT_WIFI)`
  and asks for its previous address on reconnect.
- UI: Start/Stop (USB) as today; "Share with teammates over hotspot" switch with the
  address to type and the connected count; "Hub phone address" field + "Connect to phone
  hub" button.
- Manifest: add `ACCESS_NETWORK_STATE`.
- Unit tests for `Hub.kt` run in the Docker build (`gradle test assembleDebug`).

## 5. PC relay

- `--serial` on `devices`, `install`, `run` so two phones can be connected during testing.
- Test documenting that two source addresses get independent flows.

## 6. Limits

- One USB link and one relay for everyone; browsing and chat for a handful of phones.
- Teammates see "no internet" on the Wi-Fi icon; apps work (same caveat as the host).
- Hotspot client limit and idle auto-off are phone settings.
