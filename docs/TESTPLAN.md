# On-device test plan

Unit tests and `pikioskd --simulate` run on a development machine. The checks below need the Pi, both panels, Home Assistant, and a network. Record the date, the display, and the git SHA.

## Install

- [ ] Raspberry Pi OS Bookworm 64-bit, labwc, SSH, user can log in with a password.
- [ ] `sudo ./install.sh --repo <git-url>` finishes and `pikioskd --check-config` prints ok.
- [ ] The generated admin password is printed once. `http://<pi>:8090` asks for it.
- [ ] Existing `/etc/pi-kiosk/config.yaml` is not overwritten by a second install.
- [ ] `--harden-ssh` was not required for the first login.

## Playback (M1)

- [ ] The device appears in Home Assistant from MQTT discovery, with no package YAML required.
- [ ] An approved catalog item plays full screen within 10 seconds. No YouTube page, ads, cursor, or OSD.
- [ ] Play, pause, stop, volume, and mute take effect within 1 second and show in HA within 2 seconds.
- [ ] An unknown catalog name is rejected and written to the journal.
- [ ] `systemctl --user kill pikiosk-mpv` brings mpv back within 5 seconds. HA commands still work.
- [ ] Unplug Ethernet for 2 minutes. HA shows unavailable. Replug. State returns within 60 seconds.
- [ ] Reboot returns to a black idle screen with the previous volume and mute.

## Screen and persistence (M2)

- [ ] Light off is dark within 500 ms. Light on restores brightness. Repeat on Touch Display 2 and the original 7 inch panel.
- [ ] `max_brightness` is read from sysfs, not hard-coded.
- [ ] With sleep at 1 minute and nothing playing, the panel turns off after about 60 seconds. A touch wakes it and does not start a video.
- [ ] Playback does not sleep the screen.
- [ ] After-video loop, next, stop, and blank behave for one video and for a playlist.
- [ ] `ha/packages/pikiosk.yaml` passes HA's YAML check. The media card plays, pauses, seeks, and changes volume.
- [ ] Volume, mute, brightness, sleep, after-video, UI lock, and selection survive reboot and a PoE power pull.

## Reliability (M3)

- [ ] `kill -STOP` on pikioskd causes systemd to restart it within 45 seconds.
- [ ] `kill -STOP` on mpv is detected and mpv restarts within 30 seconds.
- [ ] `systemctl --user start pikiosk-ytdlp-update` upgrades yt-dlp. A forced smoke-test failure rolls back.
- [ ] Three extractor failures trigger one yt-dlp update, and a second burst inside an hour does not.
- [ ] Editing the catalog in the admin site updates the HA select without restarting by hand.
- [ ] A bad git update that fails `/healthz` restores the previous SHA. `/etc/pi-kiosk` is unchanged.

## Offline (M4)

- [ ] With offline enabled, catalog videos download inside the window and `index.json` matches the files.
- [ ] With WAN blocked, cached videos and thumbnails still play.
- [ ] The cache stays under the size cap. Current catalog files are kept unless the cap cannot be met.
- [ ] A sync does not start while a stream is playing.

## Touch UI (M5)

- [ ] The thumbnail grid fills the panel in the right orientation. A tap plays that item. The grid returns when the video ends.
- [ ] Five minutes of taps, edge swipes, long presses, and pinches do not reveal a desktop, panel, keyboard, browser chrome, menu, or zoom.
- [ ] Touch UI off shows the locked screen within 1 second and ignores taps except wake. The lock survives reboot.
- [ ] Ten rapid taps start one video.
- [ ] With parent PIN enabled, the corner hold and the right PIN unlock for 15 minutes. Three wrong PINs cool down for 5 minutes.

## Updates

- [ ] `sudo -u kiosk /opt/pi-kiosk/bin/pikiosk-update.sh` fast-forwards when the branch moved and does nothing when it did not.
- [ ] While a video is playing, the timer skips the update. The next idle tick applies it.
- [ ] The admin Updates page and the HA `update_app` button run the same script.
