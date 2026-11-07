"""Command-line entry point for the reverse tethering relay."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from rtether import __version__
from rtether.adb import Adb, AdbError, setup_platform_tools
from rtether.dns import discover_dns_server
from rtether.server import RelayServer

DEFAULT_PORT = 31416
CONNECT_HINT_AFTER = 15.0

RELAY_DIR = Path(__file__).resolve().parents[2]
TOOLS_DIR = RELAY_DIR / ".platform-tools"
DEFAULT_APK = RELAY_DIR.parent / "dist" / "rtether.apk"

PHONE_CHECKLIST = """Phone setup (once):
  1. Settings > About phone > tap "Build number" 7 times.
  2. Settings > System > Developer options > turn on USB debugging.
  3. Plug in the cable, choose "File transfer" if asked, tap "Allow" (always) on the USB debugging prompt.
  4. After `rtether install`, open Reverse Tether on the phone, press Start, tap OK on the VPN dialog.
  5. From then on only `rtether run` is needed."""

NO_CONNECTION_HINT = (
    "No connection from the phone yet. Open Reverse Tether on the phone and press Start "
    "once to grant VPN permission."
)


def start_app_or_warn(adb) -> str | None:
    """Ask the phone to start the app; return a warning instead of failing if it cannot."""
    try:
        adb.start_app()
    except AdbError as exc:
        return (
            f"Could not start the app over adb ({exc}). Open Reverse Tether on the phone "
            "and press Start."
        )
    return None


def device_status_line(devices: list[tuple[str, str]]) -> tuple[str, int]:
    if not devices:
        return "No phone found. Check the cable and that USB debugging is on.", 2
    if any(state == "unauthorized" for _, state in devices):
        return "Phone found but not authorised. Tap Allow on the phone.", 2
    ready = [serial for serial, state in devices if state == "device"]
    if len(ready) > 1:
        return "More than one phone connected; leave only one.", 2
    if len(ready) == 1:
        return f"Phone ready: {ready[0]}", 0
    return "No phone found. Check the cable and that USB debugging is on.", 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rtether", description="Reverse USB tethering relay")
    parser.add_argument("--version", action="version", version=f"rtether {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="download Google's adb and print the phone checklist")
    sub.add_parser("devices", help="show whether the phone is ready")
    install = sub.add_parser("install", help="install the app on the phone")
    install.add_argument("--apk", type=Path, default=DEFAULT_APK)
    run = sub.add_parser("run", help="start reverse tethering")
    run.add_argument("--dns", help="upstream DNS server (default: this PC's)")
    run.add_argument("--port", type=int, default=DEFAULT_PORT)
    run.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None, make_adb=Adb.locate) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command is None:
        parser.print_help()
        return 1
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.command == "setup":
        return _cmd_setup()
    try:
        adb = make_adb(TOOLS_DIR)
    except AdbError as exc:
        print(exc)
        return 1
    if args.command == "devices":
        return _cmd_devices(adb)
    if args.command == "install":
        return _cmd_install(adb, args.apk)
    return asyncio.run(_cmd_run(adb, args.dns, args.port, args.verbose))


def _cmd_setup() -> int:
    print("Downloading Google platform-tools (adb)...")
    try:
        exe = setup_platform_tools(TOOLS_DIR)
    except (AdbError, OSError) as exc:
        print(f"Setup failed: {exc}")
        return 1
    print(f"adb ready at {exe}")
    print()
    print(PHONE_CHECKLIST)
    return 0


def _cmd_devices(adb) -> int:
    try:
        message, code = device_status_line(adb.devices())
    except AdbError as exc:
        print(exc)
        return 1
    print(message)
    return code


def _cmd_install(adb, apk: Path) -> int:
    if not apk.exists():
        print(f"APK not found at {apk}. Build it first (see README).")
        return 1
    message, code = device_status_line(adb.devices())
    if code:
        print(message)
        return code
    print(f"Installing {apk.name}...")
    try:
        print(adb.install(apk).strip())
    except AdbError as exc:
        print(exc)
        return 1
    return 0


async def _cmd_run(adb, dns: str | None, port: int, verbose: bool) -> int:
    try:
        message, code = device_status_line(adb.devices())
    except AdbError as exc:
        print(exc)
        return 1
    print(message)
    if code:
        return code
    dns_server = dns or discover_dns_server()
    print(f"Using DNS server {dns_server}")
    server = RelayServer("127.0.0.1", port, dns_server, verbose=verbose)
    try:
        adb.reverse(port)
        try:
            await server.start()
        except OSError as exc:
            print(f"Cannot listen on port {port} ({exc}). Is another rtether running?")
            return 1
        warning = start_app_or_warn(adb)
        if warning:
            print(warning)
        print("Relay running. Press Ctrl+C to stop.")
        await _wait_forever(server)
    except AdbError as exc:
        print(exc)
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for step in (adb.stop_app, lambda: adb.reverse_remove(port)):
            try:
                step()
            except AdbError:
                pass
        await server.stop()
        print("Stopped.")
    return 0


async def _wait_forever(server: RelayServer) -> None:
    waited = 0.0
    hinted = False
    announced = False
    while True:
        await asyncio.sleep(1)
        waited += 1
        if server.client_connected and not announced:
            print("Phone connected. Internet is being shared over USB.")
            announced = True
        elif not server.client_connected:
            announced = False
            if waited >= CONNECT_HINT_AFTER and not hinted:
                print(NO_CONNECTION_HINT)
                hinted = True
