# Reverse Tethering: design spec

Date: 2026-11-07
Status: approved in conversation, pending implementation plan

## 1. Goal

Give an Android phone internet access from a Windows PC over the USB cable that
already charges it. The PC has wired Ethernet only (no Wi-Fi), the phone is not
rooted. Normal USB tethering runs phone -> PC; this project runs PC -> phone.

Primary user: the author, on one PC with one phone. After it works it will be handed
to the team, so the APK must be shareable and the PC side must run with one command.

Non-goals for version 1: IPv6, ICMP/ping, multiple phones at once, a single-exe
installer for the relay, a release-signed APK.

## 2. Why an app is required

Android does not expose any API that lets an external host inject packets into a
non-rooted phone. The only sanctioned mechanism is a VpnService run by an app on the
phone. Therefore the system has two parts:

- **Android app** (`android/`): a VpnService that does nothing but copy raw IP packets
  between the tunnel and a local socket. No third-party libraries.
- **PC relay** (`relay/`): a Python program that receives those packets, terminates the
  phone's TCP/UDP flows as real sockets on the PC, and writes reply packets back.

All logic lives on the PC where it can be read and changed; the app is a dumb pipe.

## 3. Repository layout

```
reverse-tethering/
  README.md                      phone setup, build, daily use
  android/                       Gradle project, single module `app`, Kotlin
    settings.gradle.kts
    build.gradle.kts
    app/
      build.gradle.kts
      src/main/AndroidManifest.xml
      src/main/kotlin/com/laserpower/rtether/
        MainActivity.kt          one Start/Stop button, VPN consent
        TetherVpnService.kt      the VpnService
        Tunnel.kt                two copy loops + reconnect
  relay/                         Python package managed with uv
    pyproject.toml
    src/rtether/
      cli.py                     rtether setup|devices|install|run
      adb.py                     adb subprocess wrapper + platform-tools download
      framing.py                 2-byte length-prefixed frames
      packet.py                  IPv4/TCP/UDP parse, build, checksum
      tcp.py                     per-flow TCP state machine
      udp.py                     per-flow UDP relay + DNS forward
      router.py                  dispatch packets to tcp/udp
      server.py                  asyncio listener, one phone at a time
      dns.py                     discover Windows DNS servers
    tests/
      test_packet.py test_framing.py test_tcp.py test_udp.py
      test_integration.py        fake phone end-to-end against local HTTP server
  docker/
    Dockerfile.android           JDK + Android SDK, outputs app-debug.apk
  docs/superpowers/specs/        this file
```

Sibling convention followed: `pyproject.toml` + `uv.lock` like `nic-automation`.

## 4. Protocol between app and relay

- Relay listens on `127.0.0.1:31416` on the PC.
- Relay runs `adb reverse tcp:31416 tcp:31416` so that the phone's own
  `127.0.0.1:31416` reaches the relay through USB.
- App connects to `127.0.0.1:31416` (phone-initiated, so the app can retry).
- Both directions carry a stream of frames: `uint16 big-endian length` followed by
  exactly that many bytes, which are one complete IPv4 packet. Length 0 is a keepalive
  and is ignored.
- Maximum packet size 1500 bytes. The app sets the tunnel MTU to 1400 to leave room.
- Only one app connection is served at a time. A new connection replaces the old one
  and all existing flows are closed.

## 5. Android app

Package `com.laserpower.rtether`, minSdk 24 (Android 7.0), targetSdk 34, Kotlin,
no dependencies beyond the Android SDK and Kotlin stdlib. Debug-signed for now.

**TetherVpnService**

- Builder configuration: `setSession("Reverse Tether")`, `addAddress("10.8.0.2", 24)`,
  `addRoute("0.0.0.0", 0)`, `addDnsServer("10.8.0.1")`, `setMtu(1400)`,
  `setBlocking(true)`. No IPv6 address or route, so IPv6 bypasses the tunnel.
- Runs as a foreground service with a persistent notification ("Reverse tethering
  active"). On Android 14+ declares `foregroundServiceType="specialUse"`.
- Intents: action `com.laserpower.rtether.START` and `com.laserpower.rtether.STOP`.
  STOP closes the tunnel, cancels the notification, and calls `stopSelf()`.
- `Tunnel` runs two threads: tunnel-fd -> socket and socket -> tunnel-fd, using
  `FileInputStream`/`FileOutputStream` on the tunnel `ParcelFileDescriptor` and a
  plain `java.net.Socket`. On any socket error both threads stop, the socket is closed,
  and the service reconnects after 1 s, 2 s, 4 s, capped at 5 s, until STOP.
- The tunnel fd is kept open across reconnects so the phone keeps the VPN route and
  simply stalls while the relay is away.

**MainActivity**

- One button. Start: calls `VpnService.prepare(this)`; if it returns an intent, launch it
  for the consent dialog; on OK start the service with START. Stop: sends STOP.
- Shows a one-line status: "Stopped" / "Connecting to PC" / "Connected".
- The relay starts and stops the app over ADB through the activity, because the adb
  shell user is not allowed to start a `BIND_VPN_SERVICE`-protected service directly:
  `adb shell am start -n com.laserpower.rtether/.MainActivity -a com.laserpower.rtether.START`
  (and `.STOP`). The activity forwards the action to the service and closes itself.
  If VPN consent has not been granted yet, it shows the consent dialog instead.

**Permissions**: `INTERNET`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_SPECIAL_USE`,
`POST_NOTIFICATIONS` (requested at runtime on Android 13+). Service declares
`android.permission.BIND_VPN_SERVICE` and the `android.net.VpnService` intent filter.

## 6. PC relay

Python 3.12+, asyncio, no runtime dependencies outside the standard library.
Dev dependencies: pytest, pytest-asyncio, ruff.

**cli.py**, entry point `rtether`:
- `setup`: downloads `platform-tools-latest-windows.zip` from
  `https://dl.google.com/android/repository/`, verifies SHA-256 against the value
  pinned in `adb.py` (updated by hand when bumping), extracts to
  `relay/.platform-tools/`. Also prints the phone-side checklist.
- `devices`: prints `adb devices -l` with a plain-English status
  (`device` ok, `unauthorized` means "tap Allow on the phone", none means "check cable / USB debugging").
- `install`: `adb install -r` of `dist/rtether.apk`.
- `run [--dns IP] [--port N] [--verbose]`: full start sequence below. Ctrl+C stops.

Start sequence of `run`:
1. Check adb present, exactly one device in state `device`; otherwise explain and exit.
2. `adb reverse tcp:PORT tcp:PORT`.
3. Start the asyncio server.
4. Send the START intent to the app. If no app connection arrives within 15 s, print
   "open Reverse Tether on the phone and press Start once to grant VPN permission".
5. On exit: send STOP intent, `adb reverse --remove tcp:PORT`, close all flows.

**packet.py**, pure functions, no I/O:
- `parse_ipv4(bytes) -> IPv4Packet` (src, dst, protocol, payload, header length).
  Rejects non-IPv4, fragments, and bad checksums (drop silently, count in stats).
- `parse_tcp(payload) -> TCPSegment` (ports, seq, ack, flags, window, data).
- `parse_udp(payload) -> UDPDatagram`.
- `build_ipv4(...)`, `build_tcp(...)`, `build_udp(...)` producing bytes with correct
  checksums, including the TCP/UDP pseudo-header checksum.

**tcp.py**, `TCPFlow` keyed by `(src_ip, src_port, dst_ip, dst_port)`:
- Phone SYN: relay opens `asyncio.open_connection(dst_ip, dst_port)`; on success
  reply SYN-ACK (relay picks a random ISN); on failure reply RST.
- Established: phone data is written to the socket and ACKed immediately. Socket data
  is sent as PSH/ACK segments (at most MSS = 1360 bytes each), advancing our seq.
  Phone ACKs are tracked only to know when we may close.
- Duplicate or out-of-window phone segments are acknowledged and dropped.
- Phone FIN: close socket write side, reply ACK, then FIN when socket EOF reached.
  Socket EOF: send FIN; on phone's ACK of FIN remove the flow.
- Phone RST, or socket error: drop everything, send RST if appropriate.
- Fixed advertised window of 65535. Flow control: if the socket writer buffers more
  than 256 KiB, stop reading from the phone side for that flow (segments dropped and
  not ACKed, so the phone retransmits later).
- Idle timeout 5 minutes in Established, 30 s in half-closed states.

**udp.py**, `UDPFlow` keyed the same way:
- First datagram creates an asyncio datagram endpoint bound to an ephemeral port.
  Phone datagrams go out with sendto; socket replies come back via `build_udp` with
  swapped addresses. Idle timeout 60 s; DNS flows (dst port 53) timeout 10 s.
- Datagrams to `10.8.0.1:53` are rewritten to the upstream DNS server chosen in
  `dns.py` (first Windows DNS server from `Get-DnsClientServerAddress -AddressFamily IPv4`
  via PowerShell, fallback `1.1.1.1`, override with `--dns`).

**router.py**: one `Router` holding the flow tables and the write-back callback.
Dispatch on protocol: 6 is tcp, 17 is udp, anything else dropped and counted.
Periodic 10 s task evicts idle flows and logs `tcp=N udp=M rx=X tx=Y` when `--verbose`.

**server.py**: `asyncio.start_server` on `127.0.0.1:PORT`. On connect: if a client is
already active, close it and reset the router. Reads frames via `framing.py`, passes
packets to the router, and the router's write-back callback frames packets to the
client. Sends a zero-length keepalive every 10 s. Client disconnect resets the router.

## 7. Error handling summary

| Situation | Behaviour |
|---|---|
| adb missing | `run` exits with "run rtether setup first" |
| No device / unauthorized | one-line instruction, exit code 2 |
| App never connects | 15 s hint about pressing Start on the phone, keep waiting |
| Phone unplugged | all flows closed, server keeps listening, app reconnects on replug |
| Remote connect fails | RST to the phone, logged at debug |
| Malformed packet | dropped, counted, never crashes the relay |
| Ctrl+C | STOP intent, reverse removed, clean exit |

## 8. Testing

- `test_packet.py`: parse/build round trips, checksum vectors computed by hand,
  rejection of truncated and non-IPv4 input.
- `test_framing.py`: split reads, zero-length keepalive, oversized frame rejected.
- `test_tcp.py`: state machine driven with a fake stream pair; covers handshake, data
  both ways, FIN both orders, RST on connect failure, duplicate segments, backpressure.
- `test_udp.py`: echo through a local UDP server, DNS rewrite, idle eviction.
- `test_integration.py`: starts the real `server.py` on a random port, a local
  `http.server`, and a Python "fake phone" that opens the framed socket and sends
  hand-built SYN/ACK/data packets for an HTTP GET; asserts the body comes back.
  Runs on the PC with no phone, no Docker, no adb.
- Manual: phone with Wi-Fi and mobile data off, browser loads a site, WhatsApp sends
  a message, whatismyip shows the office public IP.

## 9. Build and run

- APK: `docker build -f docker/Dockerfile.android -o dist android/`
  (BuildKit output mode copies `app-debug.apk` to `dist/rtether.apk`).
- Relay: `cd relay && uv sync && uv run rtether setup && uv run rtether run`.
- Install app: `uv run rtether install` after the phone is authorised.

## 10. Phone-side checklist (manual, one time)

1. Settings > About phone > tap Build number 7 times.
2. Settings > System > Developer options > USB debugging ON.
3. Plug in, choose "File transfer" if asked, tap "Allow" (always) on the USB debugging prompt.
4. After `rtether install`, open Reverse Tether, press Start, tap OK on the VPN dialog.
5. From then on only `rtether run` on the PC is needed.
