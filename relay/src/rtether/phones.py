"""Who is connected: phones on USB cables and phones joined to a hub phone's hotspot."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field

APK_PORT = 31418
HOTSPOT_IFACE_PREFIXES = ("ap", "swlan", "softap", "wlan1")
LIVE_NEIGHBOUR_STATES = {"REACHABLE", "STALE", "DELAY", "PROBE"}

INSTALL_HINT = "Install the app on one:  uv run rtether install --serial <serial>   (or --all)"


@dataclass
class UsbPhone:
    serial: str
    state: str
    model: str = ""
    android: str = ""
    app_installed: bool = False
    hotspot_ip: str | None = None


@dataclass
class HotspotReport:
    model: str
    ip: str
    neighbours: list[tuple[str, str, str]] = field(default_factory=list)


def parse_neighbours(text: str) -> list[tuple[str, str, str]]:
    """`ip neigh show dev <iface>` -> (ip, mac, state) for live IPv4 entries."""
    found: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            ipaddress.IPv4Address(parts[0])
        except ValueError:
            continue
        state = parts[-1].upper()
        if state not in LIVE_NEIGHBOUR_STATES or "lladdr" not in parts:
            continue
        mac = parts[parts.index("lladdr") + 1]
        found.append((parts[0], mac, state.lower()))
    return found


def parse_hotspot_addr(text: str) -> tuple[str, str] | None:
    """`ip -4 -o addr show` -> (interface, ip) of the first hotspot-looking interface."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[2] != "inet":
            continue
        iface = parts[1]
        if not iface.startswith(HOTSPOT_IFACE_PREFIXES):
            continue
        return iface, parts[3].split("/")[0]
    return None


def format_report(usb: list[UsbPhone], hotspots: list[HotspotReport]) -> str:
    lines = ["USB phones"]
    if not usb:
        lines.append("  none (check the cable and USB debugging)")
    width = max((len(p.serial) for p in usb), default=0)
    for phone in usb:
        serial = phone.serial.ljust(width)
        if phone.state != "device":
            lines.append(f"  {serial}  (not authorised: tap Allow on the phone)")
            continue
        app = "app installed" if phone.app_installed else "app not installed"
        hotspot = f"hotspot on {phone.hotspot_ip}" if phone.hotspot_ip else "hotspot off"
        lines.append(f"  {serial}  {phone.model}  Android {phone.android}  {app}  {hotspot}")
    lines.append(INSTALL_HINT)
    for report in hotspots:
        lines.append("")
        lines.append(f"Phones on {report.model}'s hotspot ({report.ip})")
        if not report.neighbours:
            lines.append("  none joined yet")
        for ip, mac, state in report.neighbours:
            lines.append(f"  {ip}   {mac}   {state}")
        lines.append(
            f"Give them the app: open  http://{report.ip}:{APK_PORT}  on their phone "
            "(sharing must be on)"
        )
    return "\n".join(lines) + "\n"
