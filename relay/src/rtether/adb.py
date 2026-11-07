"""Thin wrapper over Google's adb executable, plus a one-time platform-tools download."""

from __future__ import annotations

import hashlib
import io
import subprocess
import urllib.request
import zipfile
from pathlib import Path

PLATFORM_TOOLS_URL = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
# SHA-256 of the zip as published by Google on 2026-11-07 (platform-tools 37.0.1).
PLATFORM_TOOLS_SHA256 = "45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99"

APP_PACKAGE = "com.laserpower.rtether"
SERVICE = f"{APP_PACKAGE}/.TetherVpnService"
ACTIVITY = f"{APP_PACKAGE}/.MainActivity"
ACTION_START = f"{APP_PACKAGE}.START"
ACTION_STOP = f"{APP_PACKAGE}.STOP"


class AdbError(Exception):
    """adb is missing, failed, or produced output we cannot use."""


def parse_devices(output: str) -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip() or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))
    return devices


class Adb:
    def __init__(self, exe: Path, runner=subprocess.run, serial: str | None = None):
        self.exe = Path(exe)
        self._runner = runner
        self.serial = serial

    @classmethod
    def locate(cls, tools_dir: Path, serial: str | None = None) -> Adb:
        exe = Path(tools_dir) / "platform-tools" / "adb.exe"
        if not exe.exists():
            raise AdbError("adb not found: run rtether setup first")
        return cls(exe, serial=serial)

    def run(self, *args: str, timeout: float = 30) -> str:
        target = ["-s", self.serial] if self.serial else []
        try:
            result = self._runner(
                [str(self.exe), *target, *args], capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdbError(f"adb {' '.join(args)} failed: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise AdbError(f"adb {' '.join(args)} failed: {detail}")
        return result.stdout or ""

    def devices(self) -> list[tuple[str, str]]:
        return parse_devices(self.run("devices"))

    def reverse(self, port: int) -> None:
        self.run("reverse", f"tcp:{port}", f"tcp:{port}")

    def reverse_remove(self, port: int) -> None:
        self.run("reverse", "--remove", f"tcp:{port}")

    # The adb shell user cannot start a BIND_VPN_SERVICE-protected service directly,
    # so START/STOP go through the app's own activity, which forwards them.
    def start_app(self) -> None:
        self.run("shell", "am", "start", "-n", ACTIVITY, "-a", ACTION_START)

    def stop_app(self) -> None:
        self.run("shell", "am", "start", "-n", ACTIVITY, "-a", ACTION_STOP)

    def install(self, apk: Path) -> str:
        return self.run("install", "-r", str(apk), timeout=180)

    # -- per-phone queries used by `rtether phones` ---------------------------------

    def model(self) -> str:
        return self.run("shell", "getprop", "ro.product.model").strip()

    def android_version(self) -> str:
        return self.run("shell", "getprop", "ro.build.version.release").strip()

    def app_installed(self) -> bool:
        return "package:" in self.run("shell", "pm", "path", APP_PACKAGE)

    def hotspot(self) -> tuple[str, str] | None:
        """(interface, ip) of the phone's hotspot, or None when it is off."""
        from rtether.phones import parse_hotspot_addr

        return parse_hotspot_addr(self.run("shell", "ip", "-4", "-o", "addr", "show"))

    def neighbours(self, iface: str) -> list[tuple[str, str, str]]:
        from rtether.phones import parse_neighbours

        return parse_neighbours(self.run("shell", "ip", "neigh", "show", "dev", iface))


def setup_platform_tools(
    tools_dir: Path,
    url: str = PLATFORM_TOOLS_URL,
    expected_sha: str = PLATFORM_TOOLS_SHA256,
) -> Path:
    """Download Google's platform-tools zip, verify it, and extract adb into tools_dir."""
    tools_dir = Path(tools_dir)
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected_sha.lower():
        raise AdbError(
            "platform-tools checksum mismatch; Google may have updated it. "
            "Verify and update PLATFORM_TOOLS_SHA256."
        )
    tools_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        archive.extractall(tools_dir)
    return tools_dir / "platform-tools" / "adb.exe"
