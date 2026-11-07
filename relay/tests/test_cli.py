from pathlib import Path

from rtether.adb import Adb
from rtether.cli import device_status_line, main


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out


def test_device_status_lines():
    assert device_status_line([]) == (
        "No phone found. Check the cable and that USB debugging is on.",
        2,
    )
    assert device_status_line([("ABC", "unauthorized")]) == (
        "Phone found but not authorised. Tap Allow on the phone.",
        2,
    )
    assert device_status_line([("A", "device"), ("B", "device")]) == (
        "More than one phone connected; leave only one.",
        2,
    )
    assert device_status_line([("ABC", "device")]) == ("Phone ready: ABC", 0)


class FakeAdb:
    def __init__(self, devices):
        self._devices = devices
        self.calls: list[str] = []

    def devices(self):
        return self._devices


def test_devices_command_prints_status(capsys):
    code = main(["devices"], make_adb=lambda tools_dir: FakeAdb([("ABC", "device")]))
    assert code == 0
    assert "Phone ready: ABC" in capsys.readouterr().out


def test_devices_command_reports_unauthorised(capsys):
    code = main(["devices"], make_adb=lambda tools_dir: FakeAdb([("ABC", "unauthorized")]))
    assert code == 2
    assert "Tap Allow" in capsys.readouterr().out


def test_run_requires_adb(tmp_path: Path, capsys):
    code = main(["run"], make_adb=lambda tools_dir: Adb.locate(tmp_path))
    assert code == 1
    assert "rtether setup" in capsys.readouterr().out
