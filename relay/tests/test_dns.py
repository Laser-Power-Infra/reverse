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


def test_answers_reads_a_and_aaaa_records_under_the_queried_name():
    from rtether.dns import answers

    question = b"\x07example\x03com\x00\x00\x01\x00\x01"
    cname = b"\xc0\x0c\x00\x05\x00\x01\x00\x00\x00\x3c\x00\x02\xc0\x0c"
    a = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22"
    aaaa = b"\xc0\x0c\x00\x1c\x00\x01\x00\x00\x00\x3c\x00\x10" + bytes([0x20, 0x01, 0x0d, 0xb8]) + bytes(11) + b"\x01"
    reply = b"\x12\x34\x81\x80\x00\x01\x00\x03\x00\x00\x00\x00" + question + cname + a + aaaa
    assert answers(reply) == [("example.com", "93.184.216.34"), ("example.com", "2001:db8::1")]
    assert answers(reply[:40]) == []  # truncated
    assert answers(b"\x12\x34\x01\x00" + reply[4:]) == []  # a query, not a reply
