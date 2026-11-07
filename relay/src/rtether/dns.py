"""Find the DNS server the PC itself uses, so the phone can share it."""

from __future__ import annotations

import ipaddress
import subprocess

DNS_VIRTUAL = "10.8.0.1"  # the address the phone is told to use for DNS
FALLBACK_DNS = "1.1.1.1"

_POWERSHELL_CMD = [
    "powershell",
    "-NoProfile",
    "-Command",
    "(Get-DnsClientServerAddress -AddressFamily IPv4 | "
    "Where-Object {$_.ServerAddresses}).ServerAddresses",
]


def parse_dns_output(text: str) -> str | None:
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            address = ipaddress.IPv4Address(candidate)
        except ValueError:
            continue
        if address.is_loopback:
            continue
        return str(address)
    return None


def discover_dns_server(runner=subprocess.run) -> str:
    try:
        result = runner(_POWERSHELL_CMD, capture_output=True, text=True, timeout=5)
        found = parse_dns_output(result.stdout or "")
    except Exception:
        found = None
    return found or FALLBACK_DNS
