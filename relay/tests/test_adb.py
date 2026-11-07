import hashlib
import zipfile
from pathlib import Path

import pytest

from rtether.adb import (
    ACTION_START,
    ACTION_STOP,
    APP_PACKAGE,
    SERVICE,
    Adb,
    AdbError,
    parse_devices,
    setup_platform_tools,
)


def test_constants():
    assert APP_PACKAGE == "com.laserpower.rtether"
    assert SERVICE == "com.laserpower.rtether/.TetherVpnService"
    assert ACTION_START == "com.laserpower.rtether.START"
    assert ACTION_STOP == "com.laserpower.rtether.STOP"


def test_parse_devices_states():
    out = "List of devices attached\nABC\tdevice\nXYZ\tunauthorized\n\n"
    assert parse_devices(out) == [("ABC", "device"), ("XYZ", "unauthorized")]


def test_parse_devices_empty():
    assert parse_devices("* daemon started successfully\nList of devices attached\n\n") == []


def test_locate_missing_raises(tmp_path):
    with pytest.raises(AdbError, match="run rtether setup first"):
        Adb.locate(tmp_path)


def test_locate_finds_adb(tmp_path):
    exe = tmp_path / "platform-tools" / "adb.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert Adb.locate(tmp_path).exe == exe


class FakeRun:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, args, **kwargs):
        self.calls.append([str(a) for a in args])

        class Result:
            stdout = self.stdout
            stderr = ""
            returncode = self.returncode

        return Result()


def test_reverse_builds_command(tmp_path):
    run = FakeRun()
    adb = Adb(tmp_path / "adb.exe", runner=run)
    adb.reverse(31416)
    assert run.calls[-1] == [str(tmp_path / "adb.exe"), "reverse", "tcp:31416", "tcp:31416"]
    adb.reverse_remove(31416)
    assert run.calls[-1][1:] == ["reverse", "--remove", "tcp:31416"]


def test_start_and_stop_app_commands(tmp_path):
    run = FakeRun()
    adb = Adb(tmp_path / "adb.exe", runner=run)
    adb.start_app()
    assert run.calls[-1][1:] == [
        "shell", "am", "start-foreground-service", "-n", SERVICE, "-a", ACTION_START,
    ]
    adb.stop_app()
    assert run.calls[-1][1:] == [
        "shell", "am", "start-foreground-service", "-n", SERVICE, "-a", ACTION_STOP,
    ]


def test_run_raises_on_nonzero_exit(tmp_path):
    adb = Adb(tmp_path / "adb.exe", runner=FakeRun(returncode=1))
    with pytest.raises(AdbError):
        adb.devices()


def _make_zip(tmp_path: Path) -> tuple[str, str]:
    zip_path = tmp_path / "pt.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("platform-tools/adb.exe", b"fake adb")
    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    return zip_path.resolve().as_uri(), sha


def test_setup_rejects_bad_hash(tmp_path):
    url, _ = _make_zip(tmp_path)
    with pytest.raises(AdbError, match="checksum mismatch"):
        setup_platform_tools(tmp_path / "tools", url=url, expected_sha="0" * 64)
    assert not (tmp_path / "tools" / "platform-tools" / "adb.exe").exists()


def test_setup_extracts_on_good_hash(tmp_path):
    url, sha = _make_zip(tmp_path)
    exe = setup_platform_tools(tmp_path / "tools", url=url, expected_sha=sha)
    assert exe == tmp_path / "tools" / "platform-tools" / "adb.exe"
    assert exe.read_bytes() == b"fake adb"
