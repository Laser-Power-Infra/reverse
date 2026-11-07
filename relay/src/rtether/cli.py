"""Command-line entry point for the reverse tethering relay."""

from __future__ import annotations

import argparse

from rtether import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rtether", description="Reverse USB tethering relay")
    parser.add_argument("--version", action="version", version=f"rtether {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    parser.print_help()
    return 1
