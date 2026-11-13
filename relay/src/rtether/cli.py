"""Command-line entry point for the reverse tethering relay."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from rtether import __version__
from rtether.adb import Adb, AdbError, setup_platform_tools
from rtether.dns import discover_dns_server
from rtether.phones import HotspotReport, UsbPhone, format_report
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


def device_status_line(
    devices: list[tuple[str, str]], serial: str | None = None
) -> tuple[str, int]:
    if serial:
        states = {found: state for found, state in devices if found == serial}
        if not states:
            return f"Phone {serial} not found. Check the cable and that USB debugging is on.", 2
        if states[serial] == "unauthorized":
            return "Phone found but not authorised. Tap Allow on the phone.", 2
        if states[serial] == "device":
            return f"Phone ready: {serial}", 0
        return f"Phone {serial} is {states[serial]}; reconnect the cable.", 2
    if not devices:
        return "No phone found. Check the cable and that USB debugging is on.", 2
    if any(state == "unauthorized" for _, state in devices):
        return "Phone found but not authorised. Tap Allow on the phone.", 2
    ready = [found for found, state in devices if state == "device"]
    if len(ready) > 1:
        return "More than one phone connected; leave only one or pass --serial.", 2
    if len(ready) == 1:
        return f"Phone ready: {ready[0]}", 0
    return "No phone found. Check the cable and that USB debugging is on.", 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rtether", description="Reverse USB tethering relay")
    parser.add_argument("--version", action="version", version=f"rtether {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="download Google's adb and print the phone checklist")
    devices = sub.add_parser("devices", help="show whether the phone is ready")
    sub.add_parser("phones", help="list phones on USB and on the hub phone's hotspot")
    install = sub.add_parser("install", help="install the app on the phone")
    install.add_argument("--apk", type=Path, default=DEFAULT_APK)
    install.add_argument("--all", action="store_true", help="install on every ready USB phone")
    run = sub.add_parser("run", help="start reverse tethering")
    for command in (devices, install, run):
        command.add_argument("--serial", help="which phone, when more than one is plugged in")
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
        adb = make_adb(TOOLS_DIR, getattr(args, "serial", None))
    except AdbError as exc:
        print(exc)
        return 1
    if args.command == "devices":
        return _cmd_devices(adb)
    if args.command == "phones":
        return _cmd_phones(adb, make_adb)
    if args.command == "install":
        if args.all:
            return _cmd_install_all(adb, make_adb, args.apk)
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
        message, code = device_status_line(adb.devices(), getattr(adb, "serial", None))
    except AdbError as exc:
        print(exc)
        return 1
    print(message)
    return code


def _cmd_install(adb, apk: Path) -> int:
    if not apk.exists():
        print(f"APK not found at {apk}. Build it first (see README).")
        return 1
    message, code = device_status_line(adb.devices(), getattr(adb, "serial", None))
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


def _cmd_phones(adb, make_adb) -> int:
    try:
        devices = adb.devices()
    except AdbError as exc:
        print(exc)
        return 1
    usb: list[UsbPhone] = []
    hotspots: list[HotspotReport] = []
    for serial, state in devices:
        if state != "device":
            usb.append(UsbPhone(serial, state))
            continue
        phone = make_adb(TOOLS_DIR, serial)
        try:
            hotspot = phone.hotspot()
            usb.append(
                UsbPhone(
                    serial,
                    state,
                    phone.model(),
                    phone.android_version(),
                    phone.app_installed(),
                    hotspot[1] if hotspot else None,
                )
            )
            if hotspot:
                iface, ip = hotspot
                hotspots.append(HotspotReport(usb[-1].model, ip, phone.neighbours(iface)))
        except AdbError as exc:
            usb.append(UsbPhone(serial, f"error: {exc}"))
    print(format_report(usb, hotspots), end="")
    return 0


def _cmd_install_all(adb, make_adb, apk: Path) -> int:
    if not apk.exists():
        print(f"APK not found at {apk}. Build it first (see README).")
        return 1
    try:
        devices = adb.devices()
    except AdbError as exc:
        print(exc)
        return 1
    failures = 0
    for serial, state in devices:
        if state != "device":
            print(f"Skipping {serial}: {state} (tap Allow on the phone)")
            continue
        print(f"Installing on {serial}...")
        try:
            print("  " + make_adb(TOOLS_DIR, serial).install(apk).strip())
        except AdbError as exc:
            failures += 1
            print(f"  failed: {exc}")
    return 1 if failures else 0


def quiet_resets(loop, context) -> None:
    # Windows asyncio logs an ERROR when shutting down a socket the peer already reset.
    if isinstance(context.get("exception"), ConnectionResetError):
        logging.getLogger("rtether").debug("connection reset by peer: %s", context.get("message"))
        return
    loop.default_exception_handler(context)


async def _cmd_run(adb, dns: str | None, port: int, verbose: bool) -> int:
    asyncio.get_running_loop().set_exception_handler(quiet_resets)
    try:
        message, code = device_status_line(adb.devices(), getattr(adb, "serial", None))
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
