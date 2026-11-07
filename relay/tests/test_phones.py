from rtether.phones import (
    HotspotReport,
    UsbPhone,
    format_report,
    parse_hotspot_addr,
    parse_neighbours,
)

NEIGH = (
    "10.79.54.27 lladdr aa:bb:cc:dd:ee:ff REACHABLE\n"
    "10.79.54.30 lladdr 11:22:33:44:55:66 STALE\n"
    "10.79.54.99 FAILED\n"
    "10.79.54.98 lladdr 00:00:00:00:00:01 INCOMPLETE\n"
    "fe80::1 lladdr aa:bb:cc:dd:ee:ff router REACHABLE\n"
)

ADDR = (
    "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
    "41: ap0    inet 10.79.54.103/24 brd 10.79.54.255 scope global ap0\\       valid_lft forever\n"
    "44: tun0    inet 10.8.0.2/24 scope global tun0\\       valid_lft forever\n"
)


def test_parse_neighbours_keeps_live_ipv4_entries():
    assert parse_neighbours(NEIGH) == [
        ("10.79.54.27", "aa:bb:cc:dd:ee:ff", "reachable"),
        ("10.79.54.30", "11:22:33:44:55:66", "stale"),
    ]


def test_parse_hotspot_addr_finds_the_ap_interface():
    assert parse_hotspot_addr(ADDR) == ("ap0", "10.79.54.103")
    assert parse_hotspot_addr("44: tun0    inet 10.8.0.2/24 scope global tun0\n") is None
    assert parse_hotspot_addr("") is None


EXPECTED_FULL = """USB phones
  LFEESKEUAYKRFYS4  RMX3392  Android 14  app installed  hotspot on 10.79.54.103
  ZZZ               (not authorised: tap Allow on the phone)
  OLD               Pixel 3  Android 12  app not installed  hotspot off
Install the app on one:  uv run rtether install --serial <serial>   (or --all)

Phones on RMX3392's hotspot (10.79.54.103)
  10.79.54.27   aa:bb:cc:dd:ee:ff   reachable
Give them the app: open  http://10.79.54.103:31418  on their phone (sharing must be on)
"""


def test_format_report_full():
    usb = [
        UsbPhone("LFEESKEUAYKRFYS4", "device", "RMX3392", "14", True, "10.79.54.103"),
        UsbPhone("ZZZ", "unauthorized"),
        UsbPhone("OLD", "device", "Pixel 3", "12", False, None),
    ]
    hotspots = [
        HotspotReport("RMX3392", "10.79.54.103", [("10.79.54.27", "aa:bb:cc:dd:ee:ff", "reachable")])
    ]
    assert format_report(usb, hotspots) == EXPECTED_FULL


def test_format_report_empty():
    assert format_report([], []) == (
        "USB phones\n  none (check the cable and USB debugging)\n"
        "Install the app on one:  uv run rtether install --serial <serial>   (or --all)\n"
    )


def test_format_report_hotspot_without_clients():
    report = format_report(
        [UsbPhone("A", "device", "RMX3392", "14", True, "10.79.54.103")],
        [HotspotReport("RMX3392", "10.79.54.103", [])],
    )
    assert "Phones on RMX3392's hotspot (10.79.54.103)\n  none joined yet\n" in report
