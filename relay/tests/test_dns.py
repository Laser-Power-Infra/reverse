from rtether.dns import FALLBACK_DNS, discover_dns_server, parse_dns_output


def test_parse_picks_first_ipv4():
    assert parse_dns_output("8.8.8.8\r\n8.8.4.4\r\n") == "8.8.8.8"


def test_parse_ignores_blank_and_loopback():
    assert parse_dns_output("\r\n127.0.0.1\r\n10.0.0.5") == "10.0.0.5"


def test_parse_returns_none_when_nothing_usable():
    assert parse_dns_output("") is None
    assert parse_dns_output("not an address\r\n") is None


def test_discover_falls_back_when_command_fails():
    def runner(*args, **kwargs):
        raise OSError("powershell missing")

    assert discover_dns_server(runner=runner) == FALLBACK_DNS
    assert FALLBACK_DNS == "1.1.1.1"
