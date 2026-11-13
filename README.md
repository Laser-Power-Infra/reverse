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

## Who is connected

```
cd relay && uv run rtether phones
```

prints every phone on a USB cable (serial, model, Android version, whether the app is
installed, whether its hotspot is on) and, for a phone whose hotspot is on, every phone
joined to that hotspot (IP, MAC, reachable/stale). Example:

```
USB phones
  LFEESKEUAYKRFYS4  RMX3392  Android 14  app installed  hotspot on 10.79.54.103
Install the app on one:  uv run rtether install --serial <serial>   (or --all)

Phones on RMX3392's hotspot (10.79.54.103)
  10.79.54.27   aa:bb:cc:dd:ee:ff   reachable
Give them the app: open  http://10.79.54.103:31418  on their phone (sharing must be on)
```

- USB phones: `uv run rtether install --serial <serial>` installs on one, `--all` on every
  ready phone.
- Hotspot phones: while "Share over my Wi-Fi hotspot" is on, the hub phone serves its own
  APK at `http://<hotspot ip>:31418` and shows a QR code for that link. Teammates scan it
  (or open the link in their browser), tap Download, install, and connect. A phone with nothing installed cannot be pushed to over Wi-Fi;
  this link is the way to "send" it.
- Entries can stay listed as `stale` for about a minute after a phone leaves.

## Release

Pushing a version tag makes GitHub build the APK and publish a release whose notes link
straight to the download (`.github/workflows/release.yml`).

One-time setup per GitHub repo: add the signing keystore as the secret `KEYSTORE_BASE64`
(Settings > Secrets and variables > Actions). In PowerShell this copies the value:

```
[Convert]::ToBase64String([IO.File]::ReadAllBytes("android\keystore\rtether.keystore")) | Set-Clipboard
```

Always use the same keystore file; an APK signed with a different one will not install
over the existing app. If the file is ever lost, make a new one (phones must then
uninstall the old app once):

```
docker run --rm -v "${PWD}/android/keystore:/k" gradle:8.7-jdk17 keytool -genkeypair -keystore /k/rtether.keystore -alias rtether -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Reverse Tether"
```

To release: bump `versionCode` and `versionName` in `android/app/build.gradle.kts`, commit,
then tag with the same version and push:

```
git tag v0.1.2
git push origin main v0.1.2
```

## Share with teammates (hub mode)

Android's own hotspot cannot forward a VPN, so the app does the routing itself. One phone
on USB becomes the hub; teammates join its hotspot and run the same app in client mode.

On the hub phone (the one on USB):

1. Start the USB link as usual (`uv run rtether run` on the PC).
2. Settings > Hotspot: turn the Wi-Fi hotspot on. Also turn **off** "turn off hotspot
   automatically" so it survives idle time.
3. In Reverse Tether, switch on "Share over my Wi-Fi hotspot". The screen shows the
   address teammates must type, for example `10.79.54.103:31417`, and how many are connected.
   The address changes every time the hotspot is restarted; read it from the screen.

On each teammate's phone:

1. Install `rtether.apk` (share the file; no PC needed).
2. Join the hub phone's hotspot. It will say "no internet"; that is expected.
3. Open Reverse Tether. "Hub phone address" is already filled in with the hub's address
   (edit it if it differs from the hub's screen). Tap "Connect to phone hub", and tap OK on the VPN dialog the first time.
4. Test with a normal website, not the `:31418` download link: while connected, that link
   goes through the tunnel to the PC and cannot open. Stop the app to reach it again.
5. If the phone has mobile data, Android may prefer it over the no-internet hotspot and
   drop the Wi-Fi. Turn mobile data off while using the hub.

Limits: up to 8 teammates (and whatever the phone's hotspot allows); everyone shares one
USB link and one relay, fine for browsing and chat; the same "No connection" banners from
Facebook/YouTube/Play Store apply on teammates' phones.

## Troubleshooting

| Message | What to do |
|---|---|
| `run rtether setup first` | Run `uv run rtether setup`. |
| `No phone found` | Check the cable and that USB debugging is on. Try another USB port. |
| `Phone found but not authorised` | Tap "Allow" on the phone's USB debugging prompt. |
| `More than one phone connected` | Unplug the others, or pass `--serial <id>` (see `rtether devices`). |
| Teammate cannot connect to the hub | Check they are on the hub's hotspot, the address matches the hub screen, and sharing is switched on. |
| App closes the moment Connect is tapped | That is app 0.1.0; install the update from the hub link `http://<hotspot ip>:31418`. |
| `No connection from the phone yet` | Open Reverse Tether on the phone and press Start once to grant VPN permission. |
| `APK not found` | Build it with the Docker command above. |
| Phone shows the key icon but nothing loads | Run with `--verbose`; if `tcp=` stays 0, another VPN app may be active on the phone. |

## Limits

- IPv4 only. Sites that are IPv6-only will not load.
- One phone at a time. Plugging in a second replaces the first.
- Android allows one VPN at a time, so other VPN apps must be off.
- The APK is debug-signed. Fine for the team; a release keystore is a later step.
- The phone can reach anything the PC can reach on the office network, but not
  services bound to the PC's own loopback address.
- When the VPN is the phone's only network (no SIM service, Wi-Fi off), Android marks
  the status bar with a no-network cross and some apps (Facebook, YouTube, Play Store)
  show "No connection" banners from their own Wi-Fi/mobile checks, even while their data
  flows through the tunnel (YouTube keeps playing). Browsers, WhatsApp and most apps are
  unaffected. Workaround: have any SIM registered or any Wi-Fi joined, even without a
  data plan or internet; the VPN still carries all traffic.

## Development

```
cd relay
uv run pytest        # 88 tests, no phone or Docker needed
uv run ruff check .
```

Design spec: `docs/superpowers/specs/2026-11-07-reverse-tethering-design.md`.
Implementation plan: `docs/superpowers/plans/2026-11-07-reverse-tethering.md`.

## End-to-end test record

2026-11-07, RMX3392 (Android 14), Wi-Fi off, no SIM service: Android validated the VPN as internet, example.com returned HTTP 200 from the phone shell, a 2 MB download finished in about 1 s. Relay started the app over adb and the VPN consent dialog appeared once. Not yet exercised: unplug/replug mid-page, WhatsApp.

Hub mode, same day, same phone: with sharing on, the phone listens on 31417 (hub) and 31418
(APK download). A hello over loopback was answered with 10.8.0.10; downloading
/rtether.apk returned the installed APK byte for byte; `rtether phones` lists the phone and
its hotspot.

2026-11-10, second phone (on the hotspot, app 0.1.1 installed from the hub link): joined
the hub as 10.8.0.10; the relay carried 16 TCP connections for it (Google push, Facebook,
Telegram, CDNs) alongside the hub phone's own. A fake teammate from the hub phone's shell
got a DNS answer back through hub and relay. Known: the `:31418` page is unreachable from
a connected phone because its traffic goes through the tunnel.
