"""Find the DNS server the PC itself uses, so the phone can share it."""

from __future__ import annotations

import ipaddress
import struct
import subprocess

DNS_VIRTUAL = "10.8.0.1"  # the address the phone is told to use for DNS
FALLBACK_DNS = "1.1.1.1"

_POWERSHELL_CMD = [
    "powershell",
    "-NoProfile",
    "-Command",
    (
        "(Get-DnsClientServerAddress -AddressFamily IPv4 | "
        "Where-Object {$_.ServerAddresses}).ServerAddresses"
    ),
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
    except (OSError, subprocess.SubprocessError, ValueError):
        found = None
    return found or FALLBACK_DNS


def _skip_name(payload: bytes, pos: int) -> int:
    while True:
        length = payload[pos]
        if length == 0:
            return pos + 1
        if length & 0xC0:  # compression pointer
            return pos + 2
        pos += length + 1


def answers(payload: bytes) -> list[tuple[str, str]]:
    """(queried name, address) for every A/AAAA record in a DNS reply; [] if unparseable."""
    try:
        _ident, flags, questions, count = struct.unpack_from("!HHHH", payload)
        if not flags & 0x8000 or questions != 1:
            return []
        labels = []
        pos = 12
        while payload[pos]:
            length = payload[pos]
            labels.append(payload[pos + 1 : pos + 1 + length].decode("ascii", "replace"))
            pos += length + 1
        name = ".".join(labels)
        pos += 5  # root label, type, class
        found = []
        for _ in range(count):
            pos = _skip_name(payload, pos)
            rtype, _cls, _ttl, rdlen = struct.unpack_from("!HHIH", payload, pos)
            pos += 10
            if (rtype, rdlen) in ((1, 4), (28, 16)):
                found.append((name, str(ipaddress.ip_address(payload[pos : pos + rdlen]))))
            pos += rdlen
        return found
    except (struct.error, IndexError, ValueError):
        return []
