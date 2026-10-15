# Pi Video Kiosk

A locked-down YouTube player for a Raspberry Pi. mpv and yt-dlp play the video. Home Assistant starts and stops it over MQTT. Parents edit the catalog, playlists, and broker settings in a small web admin on the LAN.

The behaviour is specified in [docs/SPEC.md](docs/SPEC.md). On-device checks are in [docs/TESTPLAN.md](docs/TESTPLAN.md).

## What you need

- Raspberry Pi 5, Raspberry Pi OS Bookworm 64-bit, labwc, and SSH.
- Either official touch display (landscape).
- Home Assistant 2025.10 or newer, with the Mosquitto add-on. MQTT without TLS is the default.
- The Pi and Home Assistant on the same LAN.

## Install over SSH

```bash
ssh kiosk@kiosk.local 'curl -fsSL https://raw.githubusercontent.com/EmileSpecs/pi-video-kiosk/main/install.sh | sudo bash -s -- --repo https://github.com/EmileSpecs/pi-video-kiosk.git'
```

The installer clones into `/opt/pi-kiosk`, creates a virtualenv, and writes `/etc/pi-kiosk/` only when those files are missing. It prints an admin password once. Open `http://kiosk.local:8090` from a phone or laptop on the LAN, set the MQTT broker, and add videos.

`--harden-ssh` is off unless you pass it, so the first password login still works.

A checkout you are developing can be installed in place:

```bash
sudo ./install.sh --dev --force
```

`--force` is only for machines that are not a Pi 5 running Bookworm.

## Update

Manual update, same script the timer runs:

```bash
ssh kiosk@kiosk.local 'sudo -u kiosk /opt/pi-kiosk/bin/pikiosk-update.sh'
```

Automatic update is on by default. Every 15 minutes `pikiosk-update.timer` fetches `update.ref` (usually `main`). It fast-forwards only. If a video is playing or loading, it waits for the next tick. After the pull it reinstalls `requirements.lock`, restarts `pikioskd`, and checks `/healthz`. A failed check resets the checkout to the previous commit. `/etc/pi-kiosk` is never touched, so a code update does not change the approved videos.

The same action is the Home Assistant button **Update app** and the admin Updates page. yt-dlp has its own daily timer.

## How a video plays

mpv never talks to YouTube. When a video is tapped, `pikioskd` asks yt-dlp to download it (`playback.ytdl_format`, H.264 up to 720p merged into one MKV) into `offline.directory`, records it in the cache index, and then tells mpv to open that local file. The touch grid and Home Assistant show the download percentage while this happens; a cached video starts at once. On boot and after every catalog save the daemon pre-caches the catalog's videos in the background so the first tap is usually instant. Playlists download their first item, start it, and append the rest as they arrive. The YouTube title of every video is kept in the cache index (taken from the download or the playlist scan, and looked up once for files cached before this was recorded), so Home Assistant and the Status page show "Rubber Duckie", not `bbbbbbbbbbb.mkv`. `offline.max_size_gb` bounds the cache; least-recently-played files go first and catalog items are kept.

YouTube occasionally requires a "PO token" even from yt-dlp's default clients. If `pikiosk-diag` shows errors mentioning `po_token` or `SABR`, install the [bgutil PO-token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) in script mode (`/opt/pi-kiosk/venv/bin/pip install bgutil-ytdlp-pot-provider`, it uses the Deno the installer already put in `/usr/local/bin`) and restart `pikioskd`. It is not installed by default.

## Admin and Home Assistant

The admin site is part of `pikioskd` on port 8090. It is how you set the broker, the catalog order, playback defaults, the screen, offline cache, and the touch grid. Passwords already saved are not sent back to the browser.

The Catalog page takes a YouTube link or id, a playlist link or id, or a video file. Files can be uploaded from the browser (MP4, MKV, WebM, MOV, M4V, AVI) and land in `playback.upload_directory` (`~kiosk/.local/share/pi-kiosk/videos`); removing such an item deletes the file. Paths typed by hand must be under `playback.file_roots`, the cache directory, or the upload directory.

The Screen page picks the orientation. Landscape is the default: the Touch Display 2 is natively portrait and gets turned 90°, the original 7" display is natively landscape and is left alone, and "mounted upside down" adds 180°. The turn is applied by labwc's autostart before mpv and Chromium start, and again by `pikioskd` whenever the setting changes; `pikiosk-orient --print` shows the transform it would use.

For a dark room, turn on Home Assistant's **Extra dim** switch, like a phone's extra-dim mode. The backlight drops to its lowest step and the Brightness slider (and the Screen light's brightness) then darkens the picture itself, from 100 % (the lowest backlight alone) down to 1 % (a tenth of that): mpv lowers the video's contrast gain (black stays black) and the touch grid gets a black overlay. Switching it off gives the slider back to the backlight at the brightness it had before. Both settings survive restarts. The Screen page's "Lowest backlight" (default 1 %) is the floor for Brightness.

Home Assistant is the remote control: play, pause, stop, next, previous, volume, mute, screen, sleep, and the touch-UI lock. Next and previous move inside the playing playlist first (previous restarts the video when more than five seconds in) and only step through the catalog at its ends. Discovery creates those entities. `ha/packages/pikiosk.yaml` adds a universal media player and example automations. `ha/dashboards/pikiosk-card.yaml` is a Lovelace starting point. If `device.id` is not `kids_kiosk`, replace that id in both files.

Useful MQTT checks from a computer with `mosquitto_sub`:

```bash
mosquitto_sub -h homeassistant.local -t 'pikiosk/#' -v
mosquitto_pub -h homeassistant.local -t pikiosk/kids_kiosk/cmd/play_media -m XqZsoesa55w
```

## Development

Python 3.10 or newer. Node is only used to compile the admin stylesheet; the Pi does not run Node.

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
npm install
npm run css
.venv\Scripts\python.exe -m ruff check pikiosk tests
.venv\Scripts\python.exe -m mypy
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m pikiosk --simulate --config config/config.example.yaml
```

On Linux, `make lint test` does the same checks. CI fails if `pikiosk/web/static/admin.css` does not match a fresh Tailwind build.

`pikioskd --check-config config/config.example.yaml` exits 0. A broken file exits 2.

## Layout

| Path | Role |
|---|---|
| `/opt/pi-kiosk` | Git checkout and virtualenv |
| `/etc/pi-kiosk/config.yaml` | Catalog and settings |
| `/etc/pi-kiosk/secrets.yaml` | MQTT password, admin password, optional PIN |
| `~kiosk/.local/state/pi-kiosk/state.json` | Volume, screen, and playback state |
| `127.0.0.1:8088` | Child grid. Chromium on the panel loads only this |
| `0.0.0.0:8090` | Parent admin |

## Troubleshooting

- `sudo -u kiosk /opt/pi-kiosk/bin/pikiosk-diag` prints the commit, versions, unit states, mpv environment, audio sinks, both log tails, and the yt-dlp format list for `ytdlp.test_id` in one go. Paste that when asking for help.
- `journalctl --user -u pikioskd -f` and `journalctl --user -u pikiosk-mpv -f`. The same output is also in `~kiosk/.cache/pi-kiosk/pikioskd.log` (rotating) and `~kiosk/.cache/pi-kiosk/mpv.log`, which is what the admin Status page tails.
- HA shows the device unavailable: the broker host in the admin Connection page, Mosquitto credentials, and that the Pi can open port 1883.
- Video fails immediately: `sudo -u kiosk /opt/pi-kiosk/bin/update-ytdlp.sh`. A cookies file can be set in `ytdlp.cookies_file` if YouTube asks for a sign-in; `ytdlp.extra_args` are passed to every yt-dlp call. The error class on the Status page tells the two cases apart: `extractor`/`bot_check` is YouTube versus yt-dlp, `player` is mpv refusing the file or the display.
- No sound: the Status page shows the audio output mpv found. "none detected" or `auto_null` means no speaker (HDMI audio, USB, or a HAT) is attached; video still plays.
- The panel stays on the desktop: labwc `~kiosk/.config/labwc/autostart` should start `pikiosk-session.target` and should not start the panel or file manager.
- Screen brightness does nothing: `ls /sys/class/backlight` and confirm the `kiosk` user is in the `video` group. The installer does not hard-code `max_brightness`.

## Remove

```bash
sudo ./uninstall.sh
sudo ./uninstall.sh --purge   # also deletes /etc/pi-kiosk
```
