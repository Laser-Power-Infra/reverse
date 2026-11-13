from pathlib import Path
from typing import ClassVar

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
        "More than one phone connected; leave only one or pass --serial.",
        2,
    )
    assert device_status_line([("ABC", "device")]) == ("Phone ready: ABC", 0)


def test_device_status_line_with_serial():
    two = [("A", "device"), ("B", "device")]
    assert device_status_line(two, serial="B") == ("Phone ready: B", 0)
    assert device_status_line(two, serial="Z") == (
        "Phone Z not found. Check the cable and that USB debugging is on.",
        2,
    )
    assert device_status_line([("B", "unauthorized")], serial="B") == (
        "Phone found but not authorised. Tap Allow on the phone.",
        2,
    )


class FakeAdb:
    def __init__(self, devices, serial=None):
        self._devices = devices
        self.serial = serial
        self.calls: list[str] = []

    def devices(self):
        return self._devices


def test_devices_command_prints_status(capsys):
    code = main(["devices"], make_adb=lambda tools_dir, serial=None: FakeAdb([("ABC", "device")]))
    assert code == 0
    assert "Phone ready: ABC" in capsys.readouterr().out


def test_devices_command_reports_unauthorised(capsys):
    code = main(
        ["devices"], make_adb=lambda tools_dir, serial=None: FakeAdb([("ABC", "unauthorized")])
    )
    assert code == 2
    assert "Tap Allow" in capsys.readouterr().out


def test_serial_selects_device(capsys):
    two = [("A", "device"), ("B", "device")]
    seen = {}

    def make_adb(tools_dir, serial=None):
        seen["serial"] = serial
        return FakeAdb(two, serial)

    assert main(["devices", "--serial", "B"], make_adb=make_adb) == 0
    assert "Phone ready: B" in capsys.readouterr().out
    assert seen["serial"] == "B"
    assert main(["devices", "--serial", "Z"], make_adb=make_adb) == 2
    assert "Phone Z not found" in capsys.readouterr().out


class FakePhone:
    """Per-serial fake used by `phones` and `install --all`."""

    installs: ClassVar[list[str]] = []

    def __init__(self, serial, model="RMX3392", version="14", installed=True, hotspot=None,
                 neighbours=()):
        self.serial = serial
        self._model = model
        self._version = version
        self._installed = installed
        self._hotspot = hotspot
        self._neighbours = list(neighbours)

    def model(self):
        return self._model

    def android_version(self):
        return self._version

    def app_installed(self):
        return self._installed

    def hotspot(self):
        return self._hotspot

    def neighbours(self, iface):
        return self._neighbours

    def install(self, apk):
        FakePhone.installs.append(self.serial)
        return "Success"


def test_phones_command_lists_usb_and_hotspot(capsys):
    devices = [("A", "device"), ("Z", "unauthorized")]
    phones = {
        "A": FakePhone("A", hotspot=("ap0", "10.79.54.103"),
                       neighbours=[("10.79.54.27", "aa:bb:cc:dd:ee:ff", "reachable")]),
    }

    def make_adb(tools_dir, serial=None):
        return FakeAdb(devices) if serial is None else phones[serial]

    assert main(["phones"], make_adb=make_adb) == 0
    out = capsys.readouterr().out
    assert "  A  RMX3392  Android 14  app installed  hotspot on 10.79.54.103" in out
    assert "  Z  (not authorised: tap Allow on the phone)" in out
    assert "Phones on RMX3392's hotspot (10.79.54.103)" in out
    assert "  10.79.54.27   aa:bb:cc:dd:ee:ff   reachable" in out
    assert "http://10.79.54.103:31418" in out


def test_install_all_installs_on_every_ready_phone(tmp_path: Path, capsys):
    apk = tmp_path / "rtether.apk"
    apk.write_bytes(b"apk")
    FakePhone.installs = []
    devices = [("A", "device"), ("B", "device"), ("Z", "unauthorized")]

    def make_adb(tools_dir, serial=None):
        return FakeAdb(devices) if serial is None else FakePhone(serial)

    assert main(["install", "--all", "--apk", str(apk)], make_adb=make_adb) == 0
    assert FakePhone.installs == ["A", "B"]
    assert "Z" in capsys.readouterr().out  # skipped phone is mentioned


def test_start_app_failure_is_a_warning_not_fatal():
    """C4: if the app cannot be started over adb, keep the relay running and tell the user."""
    from rtether.adb import AdbError
    from rtether.cli import start_app_or_warn

    class BrokenAdb:
        def start_app(self):
            raise AdbError("Error: Requires permission")

    warning = start_app_or_warn(BrokenAdb())
    assert warning is not None
    assert "press Start" in warning

    class GoodAdb:
        def start_app(self):
            pass

    assert start_app_or_warn(GoodAdb()) is None


def test_run_requires_adb(tmp_path: Path, capsys):
    code = main(["run"], make_adb=lambda tools_dir, serial=None: Adb.locate(tmp_path))
    assert code == 1
    assert "rtether setup" in capsys.readouterr().out


def test_quiet_resets_swallows_only_connection_resets():
    from rtether.cli import quiet_resets

    class Loop:
        seen = []

        def default_exception_handler(self, context):
            self.seen.append(context)

    loop = Loop()
    quiet_resets(loop, {"message": "m", "exception": ConnectionResetError()})
    assert loop.seen == []
    other = {"message": "m", "exception": ValueError()}
    quiet_resets(loop, other)
    assert loop.seen == [other]
