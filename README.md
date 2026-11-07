# Reverse Tethering

Gives an Android phone internet from a Windows PC over the USB cable that already
charges it. Normal USB tethering runs phone to PC. This runs PC to phone.

Two parts, both in this repo and both ours:

- `android/` is a tiny Kotlin app. Its VpnService captures the phone's traffic and
  copies raw packets to the PC over a port that ADB forwards through the cable.
- `relay/` is a Python program on the PC. It turns those packets into real
  connections, fetches the answers, and sends them back.

Status: code complete; see the bottom of this file for the end-to-end test record.

## Phone setup (once)

1. Settings > About phone > tap "Build number" 7 times.
2. Settings > System > Developer options > turn on **USB debugging**.
3. Plug in the cable, choose "File transfer" if asked, tick "Always allow" and tap
   "Allow" on the USB debugging prompt.
4. After `rtether install` below, open **Reverse Tether** on the phone, press
   **Start**, and tap **OK** on the VPN dialog.
5. From then on only `rtether run` on the PC is needed.

## Build the app

Docker Desktop must be running.

```
docker build -f docker/Dockerfile.android -o dist android/
```

The first build downloads the Android SDK and takes several minutes. The result is
`dist/rtether.apk`. Share that file with teammates; they do not need Docker.

## Run

```
cd relay
uv sync
uv run rtether setup      # once: downloads Google's adb, prints the phone checklist
uv run rtether devices    # should say "Phone ready: <serial>"
uv run rtether install    # once: installs dist/rtether.apk on the phone
uv run rtether run        # shares internet until Ctrl+C
```

## Daily use

Plug the phone in, then:

```
cd relay && uv run rtether run
```

A key icon appears in the phone's status bar while it is sharing. Ctrl+C stops it
and removes the icon. Unplugging and replugging the cable reconnects on its own.

Options: `--verbose` prints flow counts every 10 seconds, `--dns 8.8.8.8` overrides
the DNS server (default: the one this PC uses), `--port` changes the relay port.

## Troubleshooting

| Message | What to do |
|---|---|
| `run rtether setup first` | Run `uv run rtether setup`. |
| `No phone found` | Check the cable and that USB debugging is on. Try another USB port. |
| `Phone found but not authorised` | Tap "Allow" on the phone's USB debugging prompt. |
| `More than one phone connected` | Unplug the others. |
| `No connection from the phone yet` | Open Reverse Tether on the phone and press Start once to grant VPN permission. |
| `APK not found` | Build it with the Docker command above. |
| Phone shows the key icon but nothing loads | Run with `--verbose`; if `tcp=` stays 0, another VPN app may be active on the phone. |

## Limits

- IPv4 only. Sites that are IPv6-only will not load.
- One phone at a time. Plugging in a second replaces the first.
- Android allows one VPN at a time, so other VPN apps must be off.
- The APK is debug-signed. Fine for the team; a release keystore is a later step.

## Development

```
cd relay
uv run pytest        # 65 tests, no phone or Docker needed
uv run ruff check .
```

Design spec: `docs/superpowers/specs/2026-11-07-reverse-tethering-design.md`.
Implementation plan: `docs/superpowers/plans/2026-11-07-reverse-tethering.md`.

## End-to-end test record

Not yet run on a real phone.
