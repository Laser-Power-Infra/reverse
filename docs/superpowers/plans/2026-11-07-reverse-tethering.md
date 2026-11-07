# Reverse Tethering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a non-rooted Android phone internet from a Windows PC over USB, using our own tiny VpnService app and our own Python relay.

**Architecture:** The app copies raw IPv4 packets between Android's VPN tunnel and a localhost socket that ADB reverse-forwards to the PC. The Python relay parses those packets, terminates each phone TCP/UDP flow as a real socket on the PC, and writes reply packets back. All protocol logic is on the PC; the app is a dumb pipe.

**Tech Stack:** Python 3.12+ stdlib asyncio (relay, managed by uv), pytest + pytest-asyncio + ruff (dev), Kotlin + Android SDK 34 built in Docker (app), Google platform-tools adb.

**Spec:** `docs/superpowers/specs/2026-11-07-reverse-tethering-design.md`

## Global Constraints

- Relay: Python `>=3.12`, no runtime dependencies outside the standard library.
- Relay port `31416` on `127.0.0.1`; frames are `uint16` big-endian length + one IPv4 packet; length 0 is keepalive; max frame `1500`.
- Tunnel: address `10.8.0.2/24`, route `0.0.0.0/0`, DNS `10.8.0.1`, MTU `1400`. IPv4 only.
- TCP: MSS `1360`, window `65535`, backpressure limit `262144` bytes, idle `300` s established, `30` s half-closed.
- UDP: idle `60` s, DNS flows `10` s, DNS fallback `1.1.1.1`.
- App: package `com.laserpower.rtether`, minSdk `24`, targetSdk `34`, actions `com.laserpower.rtether.START` / `.STOP`, no third-party libraries.
- Commit after every task; commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. A TCP segment whose `ack` acknowledges bytes we never sent (phone bug or stale flow) must be ignored, not crash or corrupt `snd_una` (Task 5 test `test_ack_beyond_sent_is_ignored`).
2. A phone SYN for a flow key that already exists (phone retried after our SYN-ACK was lost) must re-send SYN-ACK, not open a second socket (Task 5 test `test_duplicate_syn_resends_synack`).
3. A UDP datagram to a port the PC cannot reach (ICMP unreachable) must not kill the flow table; the datagram is dropped and the flow keeps working (Task 6 test `test_unreachable_destination_does_not_crash`).
4. A frame carrying garbage bytes (not IPv4) in the middle of a healthy stream must be counted as dropped and the next frame still processed (Task 7 test `test_garbage_frame_is_dropped_and_stream_continues`).
5. Two app connections in a row (phone replugged) must leave zero flows from the first connection alive (Task 8 test `test_second_client_resets_router`).

---

### Task 1: Relay project scaffold

**Files:**
- Create: `relay/pyproject.toml`, `relay/src/rtether/__init__.py`, `relay/src/rtether/cli.py`, `relay/tests/__init__.py`, `relay/tests/test_cli.py`, `.gitignore`

**Interfaces:**
- Produces: console script `rtether` calling `rtether.cli:main(argv: list[str] | None = None) -> int`. `rtether.__version__ = "0.1.0"`.

- [ ] **Step 1: Write the failing test** in `relay/tests/test_cli.py`

```python
from rtether.cli import main

def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out
```

- [ ] **Step 2: Create `relay/pyproject.toml`**: name `rtether`, version `0.1.0`, `requires-python = ">=3.12"`, no dependencies, `[project.scripts] rtether = "rtether.cli:main"`, build backend `hatchling`, dev group `pytest`, `pytest-asyncio`, `ruff`; `[tool.pytest.ini_options] asyncio_mode = "auto"`, `testpaths = ["tests"]`; `[tool.ruff] line-length = 100`. Add `.gitignore` at repo root with `.venv/`, `__pycache__/`, `relay/.platform-tools/`, `dist/`, `android/.gradle/`, `android/**/build/`, `*.apk`.

- [ ] **Step 3: Run test to verify it fails**: `cd relay && uv sync && uv run pytest tests/test_cli.py -v`. Expected: FAIL, `rtether.cli` has no `main` / SystemExit.

- [ ] **Step 4: Implement `main` in `cli.py`** with `argparse`; `--version` prints `rtether 0.1.0` and returns 0. Subcommands are added in Task 10; for now `main([])` prints help and returns 1.

- [ ] **Step 5: Run test to verify it passes**: `uv run pytest -v`. Expected: 1 passed.

- [ ] **Step 6: Commit**: `git add -A && git commit -m "feat: relay project scaffold"`.

### Task 2: Framing

**Files:**
- Create: `relay/src/rtether/framing.py`, `relay/tests/test_framing.py`

**Interfaces:**
- Produces: `MAX_FRAME = 1500`; `class FrameError(Exception)`; `encode_frame(packet: bytes) -> bytes`; `async read_frame(reader: asyncio.StreamReader) -> bytes | None` (returns `b""` for a keepalive, `None` on clean EOF, raises `FrameError` on length > 1500 or truncated body).

- [ ] **Step 1: Write failing tests** in `test_framing.py`:
  - `test_encode_prefixes_big_endian_length`: `encode_frame(b"abc") == b"\x00\x03abc"`.
  - `test_encode_rejects_oversized`: `encode_frame(b"x" * 1501)` raises `FrameError`.
  - `test_read_frame_handles_split_reads`: feed `b"\x00\x05hel"` then `b"lo"` into a `StreamReader`; `await read_frame(r) == b"hello"`.
  - `test_read_keepalive_returns_empty`: `b"\x00\x00"` yields `b""`.
  - `test_read_eof_returns_none`: empty reader with `feed_eof()` yields `None`.
  - `test_read_truncated_raises`: `b"\x00\x05hi"` + EOF raises `FrameError`.
  - `test_read_oversized_raises`: `b"\x05\xdd"` raises `FrameError` before reading the body.

- [ ] **Step 2: Run tests, expect ImportError.**
- [ ] **Step 3: Implement `framing.py`** using `reader.readexactly`; map `IncompleteReadError` with zero partial bytes on the header to `None`, otherwise to `FrameError`.
- [ ] **Step 4: Run tests, expect 7 passed.**
- [ ] **Step 5: Commit** `feat: length-prefixed framing`.

### Task 3: Packet parsing and building

**Files:**
- Create: `relay/src/rtether/packet.py`, `relay/tests/test_packet.py`

**Interfaces:**
- Produces, all in `packet.py`:
  - `FlowKey = tuple[str, int, str, int]` (src_ip, src_port, dst_ip, dst_port).
  - Flag ints `FIN=0x01, SYN=0x02, RST=0x04, PSH=0x08, ACK=0x10`; `PROTO_TCP=6`, `PROTO_UDP=17`.
  - `class PacketError(ValueError)`.
  - `@dataclass(frozen=True) IPv4Packet(src: str, dst: str, protocol: int, ttl: int, payload: bytes)`.
  - `@dataclass(frozen=True) TCPSegment(src_port: int, dst_port: int, seq: int, ack: int, flags: int, window: int, data: bytes)`.
  - `@dataclass(frozen=True) UDPDatagram(src_port: int, dst_port: int, data: bytes)`.
  - `checksum(data: bytes) -> int` (RFC 1071 ones-complement, odd length padded).
  - `parse_ipv4(raw: bytes) -> IPv4Packet`, `parse_tcp(ip: IPv4Packet) -> TCPSegment`, `parse_udp(ip: IPv4Packet) -> UDPDatagram` (they take the parsed packet because the pseudo-header checksum needs the addresses).
  - `build_tcp_packet(src: str, dst: str, seg: TCPSegment, ttl: int = 64) -> bytes` and `build_udp_packet(src: str, dst: str, dgram: UDPDatagram, ttl: int = 64) -> bytes`, each returning a complete IPv4 packet with header and pseudo-header checksums, identification 0, flags/fragment 0, no options.

- [ ] **Step 1: Write failing tests** in `test_packet.py`:
  - `test_checksum_rfc1071_example`: `checksum(bytes.fromhex("0001f203f4f5f6f7")) == 0x220D`.
  - `test_parse_ipv4_wikipedia_header`: header hex `45000073000040004011b861c0a80001c0a800c7` (the Wikipedia IPv4 checksum example) plus 95 bytes of zero payload parses to `src="192.168.0.1"`, `dst="192.168.0.199"`, `protocol=17`, `ttl=64`, `len(payload)==95`.
  - `test_parse_ipv4_bad_checksum_raises`: flip one byte of the above, expect `PacketError`.
  - `test_parse_ipv4_rejects_ipv6_and_short`: `b"\x60" + b"\x00"*39` and `b"\x45"*10` both raise `PacketError`.
  - `test_parse_ipv4_rejects_fragment`: set MF flag (`0x20` in byte 6) expect `PacketError`.
  - `test_tcp_round_trip`: `build_tcp_packet("10.8.0.2","1.1.1.1", TCPSegment(40000, 443, 1, 2, SYN|ACK, 65535, b"hi"))` then `parse_ipv4` + `parse_tcp` returns an equal `TCPSegment`, and `parse_ipv4` does not raise (header checksum valid).
  - `test_tcp_checksum_verified`: corrupt a data byte of the built packet, `parse_tcp` raises `PacketError`.
  - `test_udp_round_trip`: same pattern with `UDPDatagram(5353, 53, b"\x00"*12)`.

- [ ] **Step 2: Run tests, expect ImportError.**
- [ ] **Step 3: Implement `packet.py`** with `struct` and `ipaddress`. A UDP checksum of 0 means "not computed" and is accepted.
- [ ] **Step 4: Run tests, expect 8 passed.**
- [ ] **Step 5: Commit** `feat: IPv4/TCP/UDP packet codec`.

### Task 4: DNS discovery

**Files:**
- Create: `relay/src/rtether/dns.py`, `relay/tests/test_dns.py`

**Interfaces:**
- Produces: `DNS_VIRTUAL = "10.8.0.1"`, `FALLBACK_DNS = "1.1.1.1"`, `discover_dns_server(runner=subprocess.run) -> str`, `parse_dns_output(text: str) -> str | None`.

- [ ] **Step 1: Write failing tests**: `test_parse_picks_first_ipv4` (`"8.8.8.8\r\n8.8.4.4\r\n"` gives `"8.8.8.8"`), `test_parse_ignores_blank_and_loopback` (`"\r\n127.0.0.1\r\n10.0.0.5"` gives `"10.0.0.5"`), `test_discover_falls_back_when_command_fails` (runner raises `OSError`, result `"1.1.1.1"`).
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement**: command `powershell -NoProfile -Command "(Get-DnsClientServerAddress -AddressFamily IPv4 | Where-Object {$_.ServerAddresses}).ServerAddresses"`, timeout 5 s, any exception or empty result returns `FALLBACK_DNS`.
- [ ] **Step 4: Run, expect 3 passed.**
- [ ] **Step 5: Commit** `feat: discover Windows DNS server`.

### Task 5: TCP flow state machine

**Files:**
- Create: `relay/src/rtether/tcp.py`, `relay/tests/test_tcp.py`

**Interfaces:**
- Consumes: `packet.py` types and builders.
- Produces: constants `MSS = 1360`, `WINDOW = 65535`, `BACKPRESSURE_LIMIT = 262144`; `class TCPFlow`:
  - `__init__(self, key: FlowKey, send: Callable[[bytes], None], connector: Callable[[str, int], Awaitable[tuple[StreamReader, StreamWriter]]] = asyncio.open_connection)`
  - `state: str` in `{"SYN_RECEIVED", "ESTABLISHED", "CLOSE_WAIT", "FIN_WAIT", "LAST_ACK", "CLOSED"}`
  - `last_activity: float` (monotonic), `half_closed: bool` property (true in CLOSE_WAIT/FIN_WAIT/LAST_ACK)
  - `async handle(self, seg: TCPSegment) -> None`; `close(self, send_rst: bool = False) -> None`.
  - `send` receives complete IPv4 packets built with `build_tcp_packet(key[2], key[0], ...)` (addresses swapped so the phone sees replies from the destination).

- [ ] **Step 1: Write failing tests** with a fixture that starts a local asyncio echo server on `127.0.0.1` and a `sent: list[bytes]` collector; helper `last_seg()` parses `sent[-1]`:
  - `test_syn_opens_socket_and_replies_synack`: after `handle(SYN seq=100)`, `last_seg().flags == SYN|ACK`, `ack == 101`, `state == "ESTABLISHED"` (state moves on SYN-ACK send; the phone's third ACK is accepted silently).
  - `test_syn_to_closed_port_replies_rst`: connector to an unused port, `last_seg().flags & RST`, `state == "CLOSED"`.
  - `test_data_is_relayed_and_echoed_back`: after handshake, `handle(PSH|ACK, data=b"ping")` yields an ACK with `ack == 105`, then (await `asyncio.sleep(0.05)`) a PSH|ACK segment with `data == b"ping"`.
  - `test_duplicate_segment_is_acked_and_dropped`: send the same `b"ping"` segment twice; echo server receives it once (second response segment never arrives; `sent` contains exactly one data segment).
  - `test_large_response_is_split_at_mss`: echo of 3000 bytes arrives as segments with `len(data) <= 1360`.
  - `test_phone_fin_closes_write_side_then_fins`: `handle(FIN|ACK)` gives ACK then a FIN|ACK from us once the echo server closes; `handle(ACK)` of our FIN makes `state == "CLOSED"`.
  - `test_server_eof_sends_fin`: fixture server that closes immediately after accept; after handshake our side sends FIN|ACK and `state == "FIN_WAIT"`.
  - `test_phone_rst_closes_flow`: `handle(RST)` gives `state == "CLOSED"` and the server socket is closed.
  - `test_ack_beyond_sent_is_ignored` (Review Focus 1): `handle(ACK ack=snd_nxt+5000)` leaves state and sequence numbers unchanged, no packet sent.
  - `test_duplicate_syn_resends_synack` (Review Focus 2): second identical SYN gives another SYN|ACK with the same ISN and the connector is called once.
  - `test_backpressure_stops_acking`: connector returning a writer whose `transport.get_write_buffer_size()` reports 300000; a data segment produces no ACK.

- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement `tcp.py`**. Keep `rcv_nxt`, `snd_nxt`, `snd_una`, `isn = random.randrange(2**32)`; a reader task pumps socket data to segments of at most MSS; sequence arithmetic modulo `2**32`. Segment is in-order iff `seg.seq == rcv_nxt`; otherwise ACK current `rcv_nxt` and drop.
- [ ] **Step 4: Run, expect 11 passed.**
- [ ] **Step 5: Commit** `feat: userspace TCP flow translator`.

### Task 6: UDP flow relay

**Files:**
- Create: `relay/src/rtether/udp.py`, `relay/tests/test_udp.py`

**Interfaces:**
- Consumes: `packet.py`, `dns.DNS_VIRTUAL`.
- Produces: `class UDPFlow`: `__init__(self, key: FlowKey, send: Callable[[bytes], None], dns_server: str)`, `async handle(self, dgram: UDPDatagram) -> None`, `close() -> None`, `last_activity: float`, `is_dns: bool` (dst port 53). Datagrams whose key dst is `(DNS_VIRTUAL, 53)` are sent to `(dns_server, 53)`; replies are returned to the phone with src `DNS_VIRTUAL`.

- [ ] **Step 1: Write failing tests** with a local UDP echo server fixture:
  - `test_datagram_relayed_and_reply_returned`: reply packet parses to src ip/port of the echo server and dst `10.8.0.2:40000`, data echoed.
  - `test_dns_virtual_is_rewritten`: key dst `("10.8.0.1", 53)`, `dns_server` set to the echo server's address; reply packet src is `10.8.0.1:53`.
  - `test_unreachable_destination_does_not_crash` (Review Focus 3): dst is `127.0.0.1` on a closed port; `handle` twice without exception, flow still open.
  - `test_close_releases_socket`: after `close()`, `handle` raises nothing and sends nothing.

- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement** with `loop.create_datagram_endpoint` and a `DatagramProtocol` whose `error_received` only logs.
- [ ] **Step 4: Run, expect 4 passed.**
- [ ] **Step 5: Commit** `feat: UDP flow relay with DNS rewrite`.

### Task 7: Router

**Files:**
- Create: `relay/src/rtether/router.py`, `relay/tests/test_router.py`

**Interfaces:**
- Consumes: `TCPFlow`, `UDPFlow`, `packet.py`.
- Produces: constants `TCP_IDLE = 300`, `TCP_HALF_CLOSED_IDLE = 30`, `UDP_IDLE = 60`, `DNS_IDLE = 10`; `@dataclass Stats(rx: int = 0, tx: int = 0, dropped: int = 0)`; `class Router`: `__init__(self, send: Callable[[bytes], None], dns_server: str, connector=asyncio.open_connection)`, `async handle(self, raw: bytes) -> None`, `reset() -> None` (closes every flow with RST for TCP), `evict_idle(now: float) -> int` (returns count evicted), `stats: Stats`, `tcp_flows: dict[FlowKey, TCPFlow]`, `udp_flows: dict[FlowKey, UDPFlow]`, `summary() -> str` exactly `f"tcp={len(tcp)} udp={len(udp)} rx={rx} tx={tx} dropped={dropped}"`. `send` wraps the flows' sender and increments `stats.tx`.

- [ ] **Step 1: Write failing tests**:
  - `test_tcp_syn_creates_flow`, `test_udp_creates_flow`, `test_other_protocol_dropped` (protocol 1 increments `dropped`, no flow).
  - `test_garbage_frame_is_dropped_and_stream_continues` (Review Focus 4): `handle(b"\xff"*30)` then a valid SYN; `dropped == 1`, one TCP flow.
  - `test_evict_idle_removes_old_flows`: set a flow's `last_activity` to `now - 301`, `evict_idle(now) == 1`.
  - `test_reset_closes_everything`: after reset both dicts are empty and an RST was sent for the TCP flow.
  - `test_summary_format`.

- [ ] **Step 2: Run, expect ImportError.**  - [ ] **Step 3: Implement.**  - [ ] **Step 4: Run, expect 7 passed.**  - [ ] **Step 5: Commit** `feat: packet router and flow tables`.

### Task 8: Relay server

**Files:**
- Create: `relay/src/rtether/server.py`, `relay/tests/test_server.py`

**Interfaces:**
- Consumes: `framing`, `Router`.
- Produces: `KEEPALIVE_INTERVAL = 10.0`, `EVICT_INTERVAL = 10.0`; `class RelayServer`: `__init__(self, host: str, port: int, dns_server: str, verbose: bool = False, connector=asyncio.open_connection)`, `async start() -> None`, `async stop() -> None`, `port: int` (actual bound port, so tests may pass 0), `client_connected: bool`, `router: Router`.

- [ ] **Step 1: Write failing tests**:
  - `test_client_frames_reach_router_and_replies_are_framed`: connect, send a framed UDP packet to a local echo server, read a framed reply.
  - `test_keepalive_frames_ignored`: send `b"\x00\x00"`, router stats unchanged, connection still open.
  - `test_second_client_resets_router` (Review Focus 5): first client creates a UDP flow; second client connects; `router.udp_flows == {}` and the first client's reader hits EOF.
  - `test_disconnect_resets_router`.
  - `test_oversized_frame_disconnects_client`.

- [ ] **Step 2: Run, expect ImportError.**  - [ ] **Step 3: Implement** with `asyncio.start_server`, one keepalive task and one eviction task per server, logging via `logging.getLogger("rtether")`; verbose logs `router.summary()` every eviction tick.  - [ ] **Step 4: Run, expect 5 passed.**  - [ ] **Step 5: Commit** `feat: relay server`.

### Task 9: ADB wrapper and platform-tools setup

**Files:**
- Create: `relay/src/rtether/adb.py`, `relay/tests/test_adb.py`

**Interfaces:**
- Produces: `PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"`, `PLATFORM_TOOLS_SHA256 = "<pin>"`, `APP_PACKAGE = "com.laserpower.rtether"`, `SERVICE = "com.laserpower.rtether/.TetherVpnService"`, `ACTION_START`, `ACTION_STOP`; `class AdbError(Exception)`; `parse_devices(output: str) -> list[tuple[str, str]]` (serial, state); `class Adb`: `__init__(self, exe: Path, runner=subprocess.run)`, `@classmethod locate(cls, tools_dir: Path) -> "Adb"` (raises `AdbError("run rtether setup first")`), `run(self, *args: str, timeout: float = 30) -> str`, `devices()`, `reverse(port: int)`, `reverse_remove(port: int)`, `start_app()` (`shell am start-foreground-service -n SERVICE -a ACTION_START`), `stop_app()`, `install(apk: Path)`; `setup_platform_tools(tools_dir: Path, url=PLATFORM_TOOLS_URL, expected_sha=PLATFORM_TOOLS_SHA256) -> Path` returning the adb exe path; on hash mismatch raise `AdbError` with text `"platform-tools checksum mismatch; Google may have updated it. Verify and update PLATFORM_TOOLS_SHA256."`.

- [ ] **Step 1: Write failing tests**: `test_parse_devices_states` (`"List of devices attached\nABC\tdevice\nXYZ\tunauthorized\n\n"` gives both pairs), `test_parse_devices_empty`, `test_locate_missing_raises`, `test_reverse_builds_command` (fake runner records argv `[exe, "reverse", "tcp:31416", "tcp:31416"]`), `test_setup_rejects_bad_hash` (serve a tiny zip from `tmp_path` via `file://` URL, wrong expected sha raises `AdbError`), `test_setup_extracts_on_good_hash`.
- [ ] **Step 2: Run, expect ImportError.**
- [ ] **Step 3: Implement** with `urllib.request`, `hashlib`, `zipfile`. Pin the hash: download the zip once with `curl -sLO`, run `sha256sum`, paste the value into `PLATFORM_TOOLS_SHA256`, delete the download.
- [ ] **Step 4: Run, expect 6 passed.**
- [ ] **Step 5: Commit** `feat: adb wrapper and platform-tools setup`.

### Task 10: CLI commands

**Files:**
- Modify: `relay/src/rtether/cli.py`; Test: `relay/tests/test_cli.py`

**Interfaces:**
- Consumes: `Adb`, `setup_platform_tools`, `RelayServer`, `discover_dns_server`.
- Produces: subcommands `setup`, `devices`, `install [--apk PATH]` (default `../dist/rtether.apk` relative to `relay/`), `run [--dns IP] [--port N=31416] [--verbose]`. Tools dir is `relay/.platform-tools`. Exit codes: 0 ok, 1 usage/adb missing, 2 device problem. `device_status_line(devices) -> tuple[str, int]` returns the message and exit code: no devices → `"No phone found. Check the cable and that USB debugging is on."`, 2; any `unauthorized` → `"Phone found but not authorised. Tap Allow on the phone."`, 2; more than one `device` → `"More than one phone connected; leave only one."`, 2; exactly one → `"Phone ready: <serial>"`, 0.
- `run` sequence: locate adb → device check → `reverse(port)` → start server → `start_app()` → wait; if `client_connected` is still false after 15 s print `"No connection from the phone yet. Open Reverse Tether on the phone and press Start once to grant VPN permission."` once → on Ctrl+C `stop_app()`, `reverse_remove(port)`, `server.stop()`, print `"Stopped."`.

- [ ] **Step 1: Write failing tests**: `test_device_status_lines` (four cases above), `test_devices_command_prints_status` (fake Adb injected via a `make_adb` parameter on `main`), `test_run_requires_adb` (missing tools dir → exit 1 and message contains `rtether setup`).
- [ ] **Step 2: Run, expect failures.**  - [ ] **Step 3: Implement**; `main(argv=None, make_adb=Adb.locate)`.  - [ ] **Step 4: Run full suite, expect all passed.**  - [ ] **Step 5: Commit** `feat: rtether CLI`.

### Task 11: Integration test with a fake phone

**Files:**
- Create: `relay/tests/fake_phone.py`, `relay/tests/test_integration.py`

**Interfaces:**
- Produces: `class FakePhone`: `async connect(port)`, `async http_get(dst_ip: str, dst_port: int, path: str) -> bytes` performing SYN, ACK of SYN-ACK, one PSH|ACK with the request, ACKing each reply segment, collecting data until a FIN, then ACK+FIN; `async dns_query(server_ip) -> bytes`.

- [ ] **Step 1: Write failing test** `test_http_get_through_relay`: start `http.server` on a random port serving a `tmp_path` with `hello.txt` containing `"reverse tether ok"`, start `RelayServer("127.0.0.1", 0, dns_server="127.0.0.1")`, `FakePhone.http_get("127.0.0.1", http_port, "/hello.txt")` body ends with `b"reverse tether ok"`. Second test `test_udp_echo_through_relay` with a local UDP echo server.
- [ ] **Step 2: Run, expect failure.**  - [ ] **Step 3: Implement `fake_phone.py`** with `packet.py` builders and `framing`.  - [ ] **Step 4: Run, expect 2 passed; full suite green; `uv run ruff check .` clean.**  - [ ] **Step 5: Commit** `test: end-to-end fake phone integration`.

### Task 12: Android app

**Files:**
- Create: `android/settings.gradle.kts`, `android/build.gradle.kts`, `android/gradle.properties`, `android/app/build.gradle.kts`, `android/app/src/main/AndroidManifest.xml`, `android/app/src/main/res/values/strings.xml`, `android/app/src/main/res/layout/activity_main.xml`, `android/app/src/main/kotlin/com/laserpower/rtether/{MainActivity,TetherVpnService,Tunnel}.kt`

**Interfaces:**
- Consumes: protocol constants (port 31416, frame format) from the spec.
- Produces: `TetherVpnService` responding to `ACTION_START`/`ACTION_STOP`; `Tunnel(fd: ParcelFileDescriptor, host: String = "127.0.0.1", port: Int = 31416)` with `start()`, `stop()`, `state: StateFlow<String>` values `"Connecting to PC"`, `"Connected"`, `"Stopped"`.

- [ ] **Step 1: Gradle**: AGP `8.5.2`, Kotlin `2.0.20`, `compileSdk 34`, `minSdk 24`, `targetSdk 34`, `applicationId "com.laserpower.rtether"`, dependencies only `androidx.core:core-ktx:1.13.1`, `androidx.appcompat:appcompat:1.7.0`, `org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1` (coroutines are used for `StateFlow`; they are Kotlin-official, not third-party). `gradle.properties`: `android.useAndroidX=true`, `org.gradle.jvmargs=-Xmx2g`. Gradle wrapper version `8.7` (generate the wrapper inside the Docker image in Task 13, do not commit binaries).
- [ ] **Step 2: Manifest**: permissions from spec section 5; service with `android:permission="android.permission.BIND_VPN_SERVICE"`, `android:foregroundServiceType="specialUse"`, property `android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE` = `"reverse tethering over USB"`, intent filter `android.net.VpnService`, `android:exported="true"` so ADB can start it; activity `MainActivity` launcher.
- [ ] **Step 3: `Tunnel.kt`**: two threads named `tun-to-sock` and `sock-to-tun`; reads from the tunnel with a 1500-byte buffer, writes `[len hi][len lo][packet]` to a `BufferedOutputStream`; reads frames with `DataInputStream.readUnsignedShort` + `readFully`, skips length 0, writes the packet to the tunnel; reconnect delays `1000, 2000, 4000, 5000, 5000, ...` ms until `stop()`.
- [ ] **Step 4: `TetherVpnService.kt`**: builder values from Global Constraints; notification channel `rtether`, text `Reverse tethering active`; `onStartCommand` dispatches on action; `onDestroy` and `onRevoke` stop the tunnel and close the fd.
- [ ] **Step 5: `MainActivity.kt`**: one `Button` and one `TextView`; Start path `VpnService.prepare` → `registerForActivityResult` → start service with `ACTION_START`; Stop sends `ACTION_STOP`; requests `POST_NOTIFICATIONS` on API 33+; status text bound to `Tunnel.state` via a static `StateFlow` on the service companion.
- [ ] **Step 6: Commit** `feat: android VpnService app` (build is verified in Task 13).

### Task 13: Docker APK build and README

**Files:**
- Create: `docker/Dockerfile.android`, `README.md`

- [ ] **Step 1: Dockerfile**: base `eclipse-temurin:17-jdk`, download `commandlinetools-linux-11076708_latest.zip` from `https://dl.google.com/android/repository/`, `sdkmanager "platforms;android-34" "build-tools;34.0.0" "platform-tools"` with licences accepted, install Gradle `8.7` from `services.gradle.org`, copy `android/`, run `gradle assembleDebug --no-daemon`, final stage `FROM scratch` copying `app/build/outputs/apk/debug/app-debug.apk` to `/rtether.apk`.
- [ ] **Step 2: Build**: `docker build -f docker/Dockerfile.android -o dist android/` (Docker Desktop must be running). Expected: `dist/rtether.apk` exists, size between 1 and 10 MB. Fix compile errors in Task 12 files until it builds.
- [ ] **Step 3: README**: sections "What it does", "Phone setup (once)" (spec section 10 verbatim), "Build the app", "Run" (`cd relay && uv sync && uv run rtether setup && uv run rtether install && uv run rtether run`), "Daily use" (plug in, `uv run rtether run`), "Troubleshooting" (table from spec section 7), "Limits" (IPv4 only, one phone, one VPN at a time).
- [ ] **Step 4: Commit** `build: dockerised APK build and README`.

### Task 14: Manual end-to-end on the real phone

- [ ] **Step 1**: `uv run rtether devices` → `Phone ready: <serial>`.
- [ ] **Step 2**: `uv run rtether install` → `Success`.
- [ ] **Step 3**: On the phone open Reverse Tether, press Start, tap OK on the VPN dialog; key icon appears.
- [ ] **Step 4**: `uv run rtether run --verbose`; phone Wi-Fi and mobile data off; load `https://example.com` in the browser, send a WhatsApp message, open `https://whatismyipaddress.com` and confirm the office public IP. Relay log shows `tcp=` counts rising.
- [ ] **Step 5**: Unplug and replug the cable; the page reloads without restarting the relay. Ctrl+C prints `Stopped.` and the key icon disappears.
- [ ] **Step 6**: Record results in README "Status" line and commit `docs: verified end-to-end on <phone model>`.
