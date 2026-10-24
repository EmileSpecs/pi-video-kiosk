# Pi Video Kiosk: Locked-Down YouTube Video Kiosk Controlled from Home Assistant

**Spec version:** 1.0 (2026-10-13)
**Audience:** the coding agent that will build this, and any human maintainer after it.
**Status:** Implementation-ready. The decisions below are agreed. Section 17 lists the open questions; where one is unresolved, use the default given there.

---

## 0. TL;DR

Build a Raspberry Pi 5 + official touch display appliance that plays **only parent-approved YouTube videos**, full-screen in **mpv** (via **yt-dlp**). Home Assistant controls it through **MQTT Discovery**. A single asyncio Python service (`pikioskd`) owns playback (mpv JSON IPC), the screen (backlight, blanking, idle sleep), persisted state, offline caching, and an optional local touch grid UI. The child is the user's daughter, who has Angelman syndrome, so the device must be **simple, predictable, hard to break out of, and calm**. Nothing on screen should lead anywhere else: no YouTube UI, recommendations, ads, comments, desktop, panels, keyboards, menus or error dialogs.

---

## 1. Goals and non-goals

### 1.1 Goals
1. Boots straight into the kiosk with no interaction, and recovers by itself from crashes, network loss and reboots.
2. Shows up in HA as **one device** with the full entity set (section 7), with no YAML needed in HA for the basic entities.
3. Plays YouTube videos and playlists from an **approved list**. Parents can also play an arbitrary URL or ID from HA, if config allows it.
4. Owns the screen: brightness, on/off, idle sleep, and wake on command or touch.
5. Optional offline mode: approved videos are pre-downloaded and played from local disk.
6. Optional local touch UI: a big-thumbnail grid of approved videos only, with a parent lock driven from HA.
7. Extensible: new commands, entities and sources (local files, Plex, Jellyfin and so on) can be added without restructuring.

### 1.2 Non-goals (v1)
- No YouTube account login, search, comments, subscriptions or recommendations, ever.
- No general-purpose browser exposed to the child.
- No cloud dependency other than YouTube itself (and none at all in offline mode).
- No native HA `media_player` over MQTT. HA has none; section 8 wraps the entities with `universal`.
- No audio-only or Chromecast-style casting target (possible later; see section 16).

---

## 2. Hardware and OS assumptions

| Item | Detail |
|---|---|
| SBC | Raspberry Pi 5 (4 GB or 8 GB recommended; 2 GB is fine without the touch UI) |
| Display | **Raspberry Pi Touch Display 2** (7", 720×1280 native portrait, DSI) **or** the **original 7" Touch Display** (800×480, DSI). Both must be supported. |
| Backlight | Through `/sys/class/backlight/<dev>/` (`brightness`, `max_brightness`, `bl_power`, `actual_brightness`). The device name and `max_brightness` **differ between the two displays and kernels**, so **discover them at runtime and never hard-code**. |
| Power and network | PoE HAT, single Ethernet cable. Assume an 802.3at (PoE+) switch port. 802.3af may not be enough for Pi 5 + display + USB audio (see open question Q3). |
| Audio | HDMI audio does not apply (DSI display). Expect a **USB audio device or speaker, or an I2S DAC HAT** (if it fits alongside the PoE HAT), or Bluetooth. The audio sink must be configurable (`playback.audio_device`, section 5.2). See Q4. |
| Storage | microSD (A2) or NVMe. Offline cache sizing is configurable. |
| OS | Raspberry Pi OS **Bookworm 64-bit**, desktop image, **Wayland/labwc** compositor, auto-login to the desktop as a dedicated user (default `kiosk`). |

### 2.1 Display notes the implementation must handle
- **Touch Display 2** is natively portrait. For landscape video, rotate with the compositor (`wlr-randr --output DSI-1 --transform 90`, or the labwc/kanshi output config), **and** map touch to that output in labwc `rc.xml` (`<touch deviceName="..." mapToOutput="DSI-1"/>`) so touch coordinates rotate with it. The installer writes this config; the rotation value comes from `display.rotation` in config.
- The DSI output name can be `DSI-1` or `DSI-2` depending on the Pi 5 connector used. Discover it with `wlr-randr --json` and let config override.
- `config.txt` overlays (for example `dtoverlay=vc4-kms-dsi-ili9881-7inch` for TD2 and `dtoverlay=vc4-kms-dsi-7inch` for the original, if auto-detect does not pick them up). The installer must **not** edit `config.txt` unless it is given `--display td2|legacy7`. Without the flag, it detects the display and prints guidance instead.
- Backlight semantics: `bl_power` is `0` = on and `4` = off (FB_BLANK_POWERDOWN). `brightness` runs from `0..max_brightness`. On some panels `brightness=0` is still faintly lit, so "off" must use `bl_power` (and optionally DPMS; see section 9).

### 2.2 Video decode reality on the Pi 5 (important)
The Pi 5 has **no hardware H.264 or VP9 decoder**. Its only hardware decoder is **HEVC**, which YouTube does not serve. So:
- Set mpv `hwdec=auto-safe` (harmless, and covers local HEVC files). **Do not rely on it for YouTube.**
- **Cap resolution to the panel.** Default yt-dlp format:
  `bestvideo[height<=?720][vcodec^=avc1][fps<=?30]+bestaudio[acodec^=mp4a]/bestvideo[height<=?720][fps<=?30]+bestaudio/best[height<=?720]/best`
  H.264 at 720p30 decodes comfortably in software on the Pi 5. Make `playback.ytdl_format` configurable, and use a 480p cap on the original 800×480 display.
- Use `vo=gpu` (or `gpu-next`) with `gpu-context=wayland`. Check at implementation time which one is smoother on the target OS image, and keep it configurable.

---

## 3. Architecture

```
                 ┌───────────────────── Home Assistant ─────────────────────┐
                 │  MQTT integration (discovery)  +  universal media_player  │
                 └───────────────▲───────────────────────────┬──────────────┘
                                 │ state / discovery / LWT   │ commands
                           ┌─────┴───────────────────────────▼─────┐
                           │            MQTT broker                │
                           └─────▲───────────────────────────┬─────┘
                                 │ aiomqtt                   │
┌────────────────────────────────┴───────────────────────────▼──────────────────────────┐
│ pikioskd (Python 3.11, asyncio, single process)                                        │
│                                                                                        │
│  ┌────────────┐  events  ┌──────────────┐  intents  ┌───────────────┐                  │
│  │ MqttBridge │◀───────▶│   EventBus /  │◀────────▶│ PlayerService │──JSON IPC──▶ mpv │
│  └────────────┘         │  Controller   │          └───────────────┘   (unix socket)  │
│  ┌────────────┐         │ (state store) │          ┌───────────────┐                  │
│  │ WebUI(aio- │◀──────▶│               │◀────────▶│ ScreenService │──sysfs/wlr-randr │
│  │ http + WS) │         └──────▲───────┘          └───────────────┘                  │
│  └────────────┘                │                   ┌───────────────┐                  │
│  ┌────────────┐                │                   │ InputMonitor  │──evdev (touch)   │
│  │ CacheMgr   │────────────────┤                   └───────────────┘                  │
│  │ (yt-dlp)   │                │                   ┌───────────────┐                  │
│  └────────────┘                └──────────────────▶│ Telemetry,     │                  │
│  ┌────────────┐                                    │ Persistence,   │                  │
│  │ Watchdog   │ sd_notify                          │ Catalog        │                  │
│  └────────────┘                                    └───────────────┘                  │
└────────────────────────────────────────────────────────────────────────────────────────┘
   Wayland session (labwc): mpv window (fullscreen, always-on-top when playing)
                            Chromium --kiosk → http://127.0.0.1:8088/ (touch grid, optional)
```

### 3.1 Key design rules
1. **Single source of truth.** `Controller` holds an immutable `KioskState` dataclass. Every module emits **events** and requests **intents**. Only the controller mutates state, and every change is broadcast to subscribers (MQTT publisher, WebUI WebSocket, persistence).
2. **Hardware behind interfaces.** `MpvClient`, `Backlight`, `OutputControl`, `TouchSource`, `MqttTransport` and `Clock` are protocols, each with a real and a fake implementation, so the whole service runs on a laptop with `--simulate`.
3. **mpv runs as its own systemd user unit** (`pikiosk-mpv.service`), so a crash in either process doesn't take the other down. `pikioskd` reconnects to the IPC socket with backoff.
4. **The MQTT side works without the display.** `pikioskd` starts at boot (user unit with linger) even if the compositor hasn't come up, so HA can see the device and its telemetry.
5. **No blocking calls on the event loop.** Run yt-dlp metadata and download in subprocesses (`asyncio.create_subprocess_exec`). Do sysfs writes via `asyncio.to_thread` (cheap, but keep the pattern).
6. **Commands are idempotent** and validated. Invalid commands are logged and published to `…/event` with `{"type":"error",...}`, never raised as exceptions.

### 3.2 Tech choices
| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.11 (Bookworm system Python) in a venv at `/opt/pi-kiosk/venv` | |
| MQTT | **aiomqtt ≥ 2.x** (on paho-mqtt 2.x) | asyncio-native. Implement the reconnect loop ourselves (section 12). |
| Config | PyYAML + **pydantic v2** models | Strict validation and clear errors |
| Web UI | **aiohttp** (HTTP + WebSocket), plain HTML/CSS/vanilla JS, no build step | |
| Touch | **python-evdev** | Idle reset, wake-on-touch, input grab while asleep or locked |
| Player | mpv (apt) + **yt-dlp from PyPI in the venv** (apt's is too old) + a JS runtime for yt-dlp (section 11.4) | |
| Screen | sysfs backlight; `wlr-randr` for output on/off and transform | |
| systemd | `sdnotify`-style notify (small built-in implementation over `$NOTIFY_SOCKET`; no dependency) | |
| Tests | pytest, pytest-asyncio, a fake mpv IPC server, an in-memory MQTT transport, optional mosquitto integration | |
| Lint/type | ruff, mypy (strict on `pikiosk/`) | |

---

## 4. Repository structure

```
pi-kiosk/
├── SPEC.md                          # this file
├── README.md                        # install, configure, HA setup, troubleshooting
├── LICENSE
├── pyproject.toml                   # package "pikiosk", entry points: pikioskd, pikiosk-ctl
├── requirements.lock                # pinned deps for the Pi (pip-compile output)
├── Makefile                         # lint, test, simulate, package
├── install.sh                       # idempotent installer (section 13)
├── bin/                             # update-ytdlp.sh, run-chromium.sh, wait-for-http (installed to /opt/pi-kiosk/bin)
├── docs/                            # TESTPLAN.md, soak reports, screenshots
├── uninstall.sh
├── config/
│   ├── config.example.yaml
│   └── secrets.example.yaml
├── pikiosk/
│   ├── __init__.py
│   ├── __main__.py                  # `python -m pikiosk`
│   ├── app.py                       # wiring, lifecycle, signal handling
│   ├── config.py                    # pydantic models, loader, !secret resolution
│   ├── state.py                     # KioskState, enums, reducer
│   ├── events.py                    # Event/Intent dataclasses, EventBus
│   ├── controller.py                # business logic: commands → intents → state
│   ├── catalog.py                   # approved items, ID/URL parsing, allow-list policy
│   ├── persistence.py               # atomic JSON state file
│   ├── player/
│   │   ├── __init__.py
│   │   ├── base.py                  # PlayerBackend protocol
│   │   ├── mpv_ipc.py               # async JSON IPC client (request ids, events, observe)
│   │   └── mpv_player.py            # PlayerBackend impl on top of mpv_ipc
│   ├── screen/
│   │   ├── backlight.py             # sysfs discovery + control
│   │   ├── output.py                # wlr-randr wrapper (on/off/transform, JSON parse)
│   │   └── screen_service.py        # brightness, sleep timer, wake logic
│   ├── input/
│   │   └── touch.py                 # evdev monitor, grab/ungrab
│   ├── mqtt/
│   │   ├── transport.py             # MqttTransport protocol + aiomqtt impl
│   │   ├── topics.py                # topic builder (single place for all topics)
│   │   ├── discovery.py             # HA discovery payload builders
│   │   └── bridge.py                # subscribe/dispatch commands, publish state
│   ├── cache/
│   │   ├── ytdlp.py                 # subprocess wrapper (metadata, download, version)
│   │   └── cache_manager.py         # offline cache, LRU/size limit, sync with catalog
│   ├── web/
│   │   ├── server.py                # aiohttp app, REST + WS
│   │   └── static/ (index.html, app.js, style.css, locked.html, img/)
│   ├── telemetry.py                 # cpu temp, uptime, ip, versions
│   ├── watchdog.py                  # sd_notify READY/WATCHDOG/STATUS + health checks
│   ├── logging_setup.py             # journald-friendly logging, optional file rotation
│   └── simulate.py                  # fake backends for `--simulate`
├── system/
│   ├── systemd/user/
│   │   ├── pikioskd.service
│   │   ├── pikiosk-mpv.service
│   │   ├── pikiosk-ui.service
│   │   ├── pikiosk-session.target
│   │   ├── pikiosk-ytdlp-update.service
│   │   └── pikiosk-ytdlp-update.timer
│   ├── mpv/
│   │   ├── mpv.conf
│   │   ├── input.conf               # locked-down bindings
│   │   └── scripts/                 # (optional) lua helpers, e.g. touch → script-message
│   ├── labwc/
│   │   ├── autostart                # replaces desktop autostart (no panel/desktop/keyboard)
│   │   ├── rc.xml.fragment          # window rules (mpv fullscreen + on top), touch mapping
│   │   └── environment              # XCURSOR_THEME=pikiosk-invisible, etc.
│   ├── udev/
│   │   └── 90-pikiosk-backlight.rules
│   └── cursor/pikiosk-invisible/    # transparent cursor theme
├── ha/
│   ├── packages/pikiosk.yaml        # ready-to-paste HA package (section 8)
│   └── dashboards/pikiosk-card.yaml # example Lovelace card
├── tools/
│   ├── fake_mpv.py                  # standalone fake mpv IPC server (also used by tests)
│   └── mqtt_tail.sh                 # mosquitto_sub helper
└── tests/
    ├── conftest.py
    ├── fakes/ (fake_mqtt.py, fake_mpv_server.py, fake_backlight.py, fake_clock.py, fake_touch.py)
    ├── unit/ (test_config.py, test_catalog.py, test_topics.py, test_discovery.py,
    │          test_state_reducer.py, test_backlight.py, test_sleep_timer.py,
    │          test_after_video.py, test_persistence.py, test_ytdlp_parse.py)
    ├── integration/ (test_mpv_ipc.py, test_bridge_end_to_end.py, test_web_api.py,
    │                 test_restore_after_restart.py, test_mosquitto.py [marked, optional])
    └── golden/discovery/*.json      # expected discovery payloads (snapshot tests)
```

---

## 5. Configuration

### 5.1 Files and locations
| File | Path | Mode | Purpose |
|---|---|---|---|
| Main config | `/etc/pi-kiosk/config.yaml` | 0644, root:kiosk | Everything except secrets |
| Secrets | `/etc/pi-kiosk/secrets.yaml` | **0640, root:kiosk** | MQTT password, optional cookies path, UI PIN |
| Runtime state | `~kiosk/.local/state/pi-kiosk/state.json` | 0600 | Persisted state (section 12.3) |
| Cache | `~kiosk/.cache/pi-kiosk/videos/` (configurable) | 0700 | Offline videos and thumbnails |
| IPC socket | `$XDG_RUNTIME_DIR/pi-kiosk/mpv.sock` | 0600 | mpv JSON IPC |
| Logs | journald (default); optional `~kiosk/.local/state/pi-kiosk/log/pikioskd.log` rotating | | |

Override the config path with `--config` or `PIKIOSK_CONFIG`. Values of the form `!secret key` resolve against `secrets.yaml` (HA-style). Env vars `PIKIOSK__SECTION__KEY` override any scalar (useful for tests).

### 5.2 `config.example.yaml` (complete schema with defaults)
```yaml
device:
  id: kids_kiosk                # [a-z0-9_]; used in topics and unique_ids. Immutable once deployed.
  name: "Kids Kiosk"            # HA device name → entity ids like button.kids_kiosk_play
  manufacturer: "DIY"
  model: "Raspberry Pi 5 Video Kiosk"
  suggested_area: "Bedroom"

mqtt:
  host: homeassistant.local
  port: 1883                    # 8883 when tls.enabled
  username: kiosk
  password: !secret mqtt_password
  client_id: null               # default: "pikiosk-<device.id>"
  keepalive: 30
  base_topic: pikiosk           # → pikiosk/<device.id>/...
  discovery_prefix: homeassistant
  tls:
    enabled: false
    ca_file: null
    insecure: false
  reconnect:
    min_delay_s: 1
    max_delay_s: 60
  position_publish_interval_s: 5   # throttle for the position sensor
  discovery_mode: component     # component | device (HA >= 2024.12 device-based discovery)
  legacy_object_id: false       # true for HA < 2025.10 (send object_id instead of default_entity_id)
  image_entity: false           # also expose an MQTT image entity for the thumbnail

playback:
  ytdl_format: "bestvideo[height<=?720][vcodec^=avc1][fps<=?30]+bestaudio[acodec^=mp4a]/bestvideo[height<=?720][fps<=?30]+bestaudio/best[height<=?720]/best"
  default_volume: 40            # 0-100
  max_volume: 80                # hard cap, enforced for every source (child safety / hearing)
  start_muted: false
  after_video: stop             # loop | next | stop | blank
  resume_on_boot: false         # true = resume the last item at its last position after reboot
  resume_position: true         # remember per-video position and resume (approved items only)
  allow_unlisted: true          # allow HA "play URL/ID" for items not in the catalog
  allowed_hosts: [youtube.com, www.youtube.com, m.youtube.com, youtu.be, music.youtube.com]
  prefer_offline: true          # use the cached file when present
  audio_device: auto            # mpv --audio-device value, e.g. "pipewire" or "alsa/plughw:CARD=Device"
  network_timeout_s: 30
  after_video_scope: playlist   # item | playlist (when stop/blank apply inside playlists)
  play_limit_enabled: false     # stop playback and turn the screen off after one session
  play_limit_min: 30            # 1-240; countdown starts when playback begins, not on next/pause

screen:
  backlight_device: auto        # auto | name under /sys/class/backlight
  output_name: auto             # auto | DSI-1 | DSI-2
  rotation: 0                   # 0 | 90 | 180 | 270 (compositor transform)
  default_brightness: 70        # percent
  min_brightness: 1             # percent; "on" never goes below this, and never below sysfs 1
  blank_method: backlight       # backlight | output | both (see section 9)
  sleep_enabled: true
  sleep_timeout_min: 10         # idle minutes before sleep (only when not playing)
  wake_on_touch: true
  swallow_wake_touch: true      # the touch that wakes the screen is not delivered to apps
  dim_before_sleep_s: 15        # dim to min_brightness this long before sleep; 0 disables

ui:
  enabled: true                 # run Chromium kiosk with the touch grid
  bind: 127.0.0.1
  port: 8088
  locked: false                 # default lock state (HA switch overrides, persisted)
  locked_screen: black          # black | logo | clock   (what is shown when locked and idle)
  touch_during_playback: ignore # ignore | pause_toggle | stop_to_grid
  long_press_stop_s: 0          # >0: long press during playback returns to grid (0 = disabled)
  columns: 3
  admin_enabled: false          # read-only /admin status page (only if bind is not loopback)
  show_titles: false            # big thumbnails only by default
  parent_unlock:
    enabled: false              # on-device unlock gesture (corner hold + PIN)
    hold_corner: top_right
    hold_s: 5
    pin: !secret ui_pin

offline:
  enabled: false
  directory: ~/.cache/pi-kiosk/videos
  max_size_gb: 20
  sync_interval_h: 12           # re-check playlists and download new approved items
  download_window: "01:00-06:00"  # only download in this local window (null = any time)
  rate_limit: null              # e.g. "2M" → yt-dlp --limit-rate
  only_offline: false           # true: never stream, play cache only (fully offline mode)

ytdlp:
  update_channel: stable        # stable | nightly
  extra_args: []                # appended to every yt-dlp call
  cookies_file: null            # optional, only if YouTube demands sign-in ("confirm you're not a bot")

input:
  touch_device: auto            # auto | /dev/input/by-path/...

telemetry:
  interval_s: 60

logging:
  level: INFO                   # DEBUG | INFO | WARNING | ERROR
  file: null                    # optional path for rotating file log
  json: false

catalog:
  # Approved content. Order = order shown in UI and the HA select, and used for next/previous.
  # Each item: type video|playlist, id (YouTube id) or url, display name (unique), optional thumbnail override.
  items:
    - name: "Baby Shark"
      type: video
      id: XqZsoesa55w
    - name: "Sesame Street Songs"
      type: playlist
      id: PLxxxxxxxxxxxxxxxxxxxxxxxxxxxx
      shuffle: false
      max_items: 50
    - name: "Bath song"
      type: video
      url: "https://youtu.be/WRVsOCh907o"
      start: 3                  # optional start offset seconds
      thumbnail: /etc/pi-kiosk/thumbs/bath.jpg   # optional local override
    - name: "Calm lights (local)"
      type: file
      path: /home/kiosk/Videos/calm.mp4
```

### 5.3 `secrets.example.yaml`
```yaml
mqtt_password: "change-me"
ui_pin: "2468"
```

### 5.4 Validation rules
- `device.id` must match `^[a-z0-9_]{3,32}$`. Item `name`s must be unique and 1–64 chars. A video `id` must match `^[A-Za-z0-9_-]{11}$`. A playlist `id` must match `^(PL|UU|LL|FL|OL|RD)[A-Za-z0-9_-]+$` (be lenient but non-empty).
- Need exactly one of `id`, `url` or `path` per item. A `url` is parsed into an `id` with the catalog parser (section 6.6).
- `max_volume ≥ default_volume`. Percentages are 0–100.
- A config error at startup means: log a clear message, publish nothing, and exit code 2. systemd retries with a slow restart (`RestartSec=15`), and the error appears in `journalctl`.
- **Hot reload:** on `SIGHUP` or the HA button "Reload config", re-read and validate. If it's valid, apply it and republish discovery (select options change). If not, keep the old config and publish an error event.

---

## 6. Core behaviour

### 6.1 State model (`state.py`)
```python
class PlayerState(StrEnum):   # published as sensor "state"
    IDLE = "idle"             # nothing loaded (grid or blank shown)
    LOADING = "loading"       # resolving/buffering before first frame
    PLAYING = "playing"
    PAUSED = "paused"
    ERROR = "error"           # last load failed; auto-clears to idle after 10 s
    OFFLINE = "offline"       # mpv not connected (internal; HA sees availability)

class AfterVideo(StrEnum): LOOP="loop"; NEXT="next"; STOP="stop"; BLANK="blank"

@dataclass(frozen=True)
class NowPlaying:
    source: Literal["catalog","adhoc","file"]
    catalog_name: str | None   # matches select option when from catalog
    video_id: str | None
    playlist_id: str | None
    playlist_index: int | None # 0-based within playlist
    playlist_count: int | None
    title: str
    duration: float | None     # seconds
    position: float | None     # seconds
    thumbnail_url: str | None
    local_file: str | None     # set when playing from cache

@dataclass(frozen=True)
class KioskState:
    player: PlayerState
    now: NowPlaying | None
    volume: int                # 0..100 (already clamped to max_volume)
    muted: bool
    after_video: AfterVideo
    screen_on: bool
    brightness: int            # 0..255 HA scale (last "on" brightness kept while off)
    sleep_enabled: bool
    sleep_timeout_min: int
    ui_locked: bool
    selected: str | None       # current select option (catalog name) or None
    last_error: str | None
    mpv_connected: bool
    mqtt_connected: bool
```

### 6.2 Commands (internal API; MQTT, Web UI and CLI all map onto these)
| Command | Args | Behaviour |
|---|---|---|
| `play` | — | If paused: unpause. If idle and `selected` is set: play `selected`. If idle with nothing selected: play catalog item 0. Wakes the screen. |
| `pause` | — | Pause if playing. No-op otherwise. |
| `play_pause` | — | Toggle (used by touch policy and the media_player). |
| `stop` | — | Stop playback (mpv `stop`), go to idle, show the grid (or the locked screen) and start the idle timer. |
| `next` / `previous` | — | Inside a playlist: mpv `playlist-next` / `playlist-prev`. Otherwise: the next or previous **catalog** item (wrapping). `previous` restarts the current item when position > 5 s (standard player behaviour). |
| `select` | `name` | Set `selected` and **start playing it** (agreed: selecting is playing). An unknown name is rejected. |
| `play_media` | `ref` (URL / 11-char ID / catalog name / `file:` path), optional `start`, `playlist_index` | Resolve with the catalog parser and enforce the policy (`allow_unlisted`, `allowed_hosts`). Load and play. Wakes the screen. |
| `seek` | `position` seconds | Absolute seek (from the HA media_player). |
| `set_volume` | `0..100` | Clamp to `max_volume`. Persist. |
| `set_mute` | bool | Persist. |
| `screen` | `on: bool`, optional `brightness 0..255` | Section 9. |
| `set_sleep_enabled` | bool | Persist. Reset the timer. |
| `set_sleep_timeout` | minutes `1..240` | Persist. Reset the timer. |
| `set_after_video` | enum | Persist. Applies to the current item right away (loop toggles `loop-file`). |
| `set_ui_locked` | bool | Section 10. Persist. |
| `reload_config`, `restart_service`, `update_ytdlp`, `sync_offline` | — | Maintenance buttons (category `config`/`diagnostic`). |

### 6.3 Playback via mpv JSON IPC
- **mpv** runs permanently with `--idle=yes --force-window=no --input-ipc-server=$XDG_RUNTIME_DIR/pi-kiosk/mpv.sock`. With `force-window=no`, **no mpv window exists while idle**, so the Chromium grid (or a black background) shows. When a file loads, mpv opens its window, and the labwc window rule makes it fullscreen and always-on-top (section 13.5). When playback stops, the window closes.
- The IPC client (`mpv_ipc.py`) must:
  - Speak newline-delimited JSON. Use `request_id` (monotonic int) per command and resolve futures on the matching reply. Time out each request after 5 s.
  - Fan out unsolicited `event` messages to subscribers.
  - Use `observe_property` (with stable observer ids) for: `pause`, `idle-active`, `core-idle`, `paused-for-cache`, `media-title`, `duration`, `time-pos` (throttled by us), `playlist-pos`, `playlist-count`, `volume`, `mute`, `eof-reached`, `path`.
  - Handle events: `start-file`, `file-loaded`, `end-file` (with `reason`: `eof|stop|quit|error|redirect`, plus `file_error`), `playback-restart`, `seek`, `client-message` (touch from input.conf, section 10.3), `shutdown`.
  - Reconnect with backoff (0.5 s → 10 s) when the socket is missing or closed. On reconnect, re-issue `observe_property` and re-apply volume, mute and loop state from `KioskState`.
- **Loading:**
  - Catalog video: `loadfile https://www.youtube.com/watch?v=<id> replace` with per-file options `start=<s>` if needed. A local cache hit uses `loadfile <path>`.
  - Catalog playlist: `loadfile https://www.youtube.com/playlist?list=<id>`. mpv's ytdl_hook expands it into the mpv playlist. Set `ytdl-raw-options` with `yes-playlist=` and `playlist-items=1-<max_items>`. Shuffle uses mpv `playlist-shuffle` after load.
  - **Never** load watch URLs that carry `list=` from ad-hoc input unless the item is a playlist. Strip tracking parameters (`si`, `pp`, `feature`).
- **Title, duration, thumbnail:** take the title from `media-title` (yt-dlp provides it) and the duration from `duration`. Use `https://i.ytimg.com/vi/<id>/hqdefault.jpg` as the thumbnail (no API call needed). For playlist entries, get the current entry's id from `path` or `playlist/N/filename`. For local files, use `http://<pi-ip>:<ui.port>/thumbs/<hash>.jpg`, generated at cache time.
- **After-video behaviour.** When an `end-file` with `reason=eof` arrives (or, for loop, never fires):
  - `loop`: set `loop-file=inf` as soon as the file loads (and `loop-playlist=inf` for playlists).
  - `next`: playlist → mpv advances by itself; at the playlist end, go to the next catalog item. Single video → next catalog item.
  - `stop`: return to idle (grid) and start the idle timer.
  - `blank`: stop + screen off (as if the sleep timer fired, but straight away).
  - Inside playlists, `stop`/`blank` apply at the **end of the playlist**, not after each entry. Make this configurable later via `after_video_scope: item|playlist`; default `playlist`.
- **Errors:** On `end-file reason=error` (yt-dlp failure, network, geo, age-gate), set `state=error` and `last_error` (short text), publish an event, and retry once after 3 s. If a cached copy exists, fall back to it. After 10 s, return to idle and show the grid. Never show mpv's OSD error text to the child (`osd-level=0`).
- **Volume:** keep mpv at `volume-max=100`. The service maps 0..100 straight to mpv volume, after clamping to `max_volume`.

### 6.4 Idle and sleep timer
- The timer runs **only when** `sleep_enabled` is true **and** `player ∈ {idle, paused, error}` **and** `screen_on`.
- It resets on: touch (evdev), any command from MQTT or the UI, the start or resumption of playback, and the screen being turned on.
- Paused for more than the timeout counts as idle (so a paused video sleeps, which is intended for bedtime). Open question Q8.
- When `dim_before_sleep_s > 0`, dim to `min_brightness` that long before sleeping, and restore the brightness on any reset.
- On expiry: screen off (section 9), publish `screen_on=false`, and leave playback state unchanged (paused stays paused).
- Implement it with an injectable `Clock` and a single rescheduled `asyncio.TimerHandle` (no polling), so the tests are deterministic.

### 6.5 Wake rules
- Any of these turns the screen on and resets the timer: HA light ON, `play`, `play_media`, `select`, `next`/`previous`, and touch (if `wake_on_touch`).
- When `swallow_wake_touch` is set, the waking touch is consumed: while the screen is off, `InputMonitor` **grabs** the touch device (`EVIOCGRAB`), and it releases the grab ~300 ms after that touch is lifted. That stops a sleeping-screen tap from accidentally starting a video.

### 6.6 Catalog and reference parsing (`catalog.py`)
- `parse_ref(s) → Ref(kind=video|playlist|file|catalog, id, start)` accepts:
  - an 11-char video ID
  - `youtube.com/watch?v=…[&t=…]`
  - `youtu.be/<id>[?t=…]`
  - `youtube.com/shorts/<id>` and `youtube.com/embed/<id>`
  - `youtube.com/live/<id>`
  - `music.youtube.com/watch?v=…`
  - `youtube.com/playlist?list=…`
  - `file:/abs/path` (only under the allowed directories `offline.directory` and `/home/kiosk/Videos`)
  - an exact catalog item name
- The `t` / `start` formats `90`, `90s` and `1m30s` all parse to seconds.
- Policy: if a ref isn't in the catalog and `allow_unlisted=false`, reject it with `error: not_allowed`. Hosts must be in `allowed_hosts`. Reject everything else (http to random hosts, `ytdl://` search terms like `ytsearch:`, which must **never** run).
- Ad-hoc plays are **never** added to the UI grid (the grid shows approved items only).
- Expose `catalog.items` with resolved thumbnails (local override → cached thumbnail → ytimg URL proxied through the web server so the UI works offline once cached).

---

## 7. MQTT interface and Home Assistant Discovery

### 7.1 Conventions
- `B = <mqtt.base_topic>/<device.id>`, for example `pikiosk/kids_kiosk`.
- `D = <mqtt.discovery_prefix>`, for example `homeassistant`.
- Discovery topic: `D/<component>/pikiosk_<device.id>/<object>/config` (per-component discovery, retained).
- `unique_id = pikiosk_<device.id>_<object>`, with `has_entity_name: true` and a short `name`, so entity ids come out as `<domain>.kids_kiosk_<object>`. Also set `default_entity_id` (HA ≥ 2025.10, which replaced `object_id`) to `<domain>.<device.id>_<object>`, so entity ids are deterministic and the HA YAML in section 8 works without editing. If the HA version is older than 2025.10, send `object_id: <device.id>_<object>` instead (config flag `mqtt.legacy_object_id: true`).
- QoS 1 for commands, discovery and state. Discovery, state, telemetry and availability are **retained**. Commands and events are **not retained**. The service must **ignore retained command messages** (check the `retain` flag on receive) so stale commands don't replay on reconnect.
- Payload encoding is UTF-8. JSON is compact (`separators=(",",":")`).
- Re-publish all discovery payloads, then the full state: (a) on every (re)connect, (b) when `D/status` (HA birth topic) receives `online`, (c) after a config reload. To **remove** entities that disappeared from config (for example a renamed device or a removed maintenance button), publish an empty retained payload to their old config topics (track the published set in `state.json`).
- Optional: device-based discovery (`D/device/<id>/config` with a `cmps` map, HA ≥ 2024.12) behind `mqtt.discovery_mode: device|component`. The default is `component` for compatibility. Both builders share the same entity definitions.

### 7.2 Topic layout (exact)

| Topic | Dir | Retained | Payload |
|---|---|---|---|
| `B/status` | out | yes | `online` / `offline`. **LWT** = `offline`, set in CONNECT. `online` is published after discovery. Graceful shutdown publishes `offline`. |
| `B/player/status` | out | yes | `online` / `offline`. Reflects the mpv IPC connection. Player entities use it as a second availability topic. |
| `B/state` | out | yes | Player JSON (below). Published on change, and `position` changes are throttled to `position_publish_interval_s`. |
| `B/screen/state` | out | yes | Light JSON: `{"state":"ON","brightness":178,"color_mode":"brightness"}` |
| `B/telemetry` | out | yes | System JSON (below), every `telemetry.interval_s` |
| `B/event` | out | no | `{"type":"error\|info\|video_started\|video_ended\|touch\|sleep\|wake","ts":"…","data":{…}}` |
| `B/cmd/play` | in | no | `PRESS` |
| `B/cmd/pause` | in | no | `PRESS` |
| `B/cmd/stop` | in | no | `PRESS` |
| `B/cmd/next` | in | no | `PRESS` |
| `B/cmd/previous` | in | no | `PRESS` |
| `B/cmd/play_pause` | in | no | `PRESS` (no button entity; used by the media_player and automations) |
| `B/cmd/select` | in | no | catalog item name (exactly one of the select options) |
| `B/cmd/play_media` | in | no | Plain string (URL / ID / catalog name), **or** JSON `{"ref":"…","start":30,"volume":40,"after_video":"stop"}` |
| `B/cmd/seek` | in | no | seconds (float) |
| `B/cmd/volume` | in | no | `0`–`100` |
| `B/cmd/mute` | in | no | `ON` / `OFF` |
| `B/cmd/screen` | in | no | Light JSON: `{"state":"ON"}`, `{"state":"ON","brightness":120}`, `{"state":"OFF"}` |
| `B/cmd/sleep_timeout` | in | no | `1`–`240` (minutes) |
| `B/cmd/sleep_enabled` | in | no | `ON` / `OFF` |
| `B/cmd/after_video` | in | no | `loop` / `next` / `stop` / `blank` |
| `B/cmd/ui_enabled` | in | no | `ON` (show touch UI = unlocked) / `OFF` (locked) |
| `B/cmd/reload_config` | in | no | `PRESS` |
| `B/cmd/update_ytdlp` | in | no | `PRESS` |
| `B/cmd/sync_offline` | in | no | `PRESS` |
| `B/cmd/restart` | in | no | `PRESS` (restarts the services, not the Pi) |
| `B/cmd/json` | in | no | Generic extensibility endpoint: `{"command":"<name>","args":{…}}`. Any internal command (section 6.2) is accepted here, so new features need no new topic. |
| `D/status` | in | — | HA birth (`online`) → republish discovery and state |

**`B/state` payload:**
```json
{
  "state": "playing",
  "title": "Baby Shark Dance",
  "position": 42.0,
  "duration": 136.5,
  "thumbnail": "https://i.ytimg.com/vi/XqZsoesa55w/hqdefault.jpg",
  "media_id": "XqZsoesa55w",
  "media_url": "https://www.youtube.com/watch?v=XqZsoesa55w",
  "source": "catalog",
  "selected": "Baby Shark",
  "playlist_index": null,
  "playlist_count": null,
  "offline": false,
  "volume": 40,
  "muted": false,
  "after_video": "stop",
  "sleep_enabled": true,
  "sleep_timeout": 10,
  "ui_enabled": true,
  "last_error": null,
  "updated": "2026-10-13T18:33:00Z"
}
```
Use `null` (not missing) for unknown fields. When idle: `title=""`, `position=null`, `duration=null`, `thumbnail=null`, and `selected` keeps the last selection (so the HA select doesn't flicker to "unknown").

**`B/telemetry` payload:**
```json
{
  "cpu_temp": 52.3,
  "boot_time": "2026-10-13T04:01:12+00:00",
  "uptime_s": 52310,
  "ip": "192.168.1.57",
  "hostname": "kiosk",
  "version": "1.0.0",
  "ytdlp_version": "2026.10.10",
  "mpv_version": "0.35.1",
  "cache_used_gb": 4.2,
  "cache_items": 31,
  "throttled": "0x0",
  "load_1m": 0.42
}
```
Sources: CPU temperature from `/sys/class/thermal/thermal_zone0/temp` divided by 1000. Boot time from `/proc/stat` btime, and uptime from `/proc/uptime`. IP is the source address of the default route (open a UDP socket to the broker host and call `getsockname`; no traffic is sent), falling back to the first global IPv4 on `eth0`. `throttled` comes from `vcgencmd get_throttled`, if present (useful for spotting PoE power problems).

### 7.3 Shared discovery fragments
Every payload includes these (shown once here; the builder injects them):
```json
{
  "availability": [
    {"topic": "pikiosk/kids_kiosk/status"}
  ],
  "availability_mode": "all",
  "device": {
    "identifiers": ["pikiosk_kids_kiosk"],
    "name": "Kids Kiosk",
    "manufacturer": "DIY",
    "model": "Raspberry Pi 5 Video Kiosk",
    "sw_version": "1.0.0",
    "hw_version": "Raspberry Pi 5 Model B Rev 1.0 / Touch Display 2",
    "suggested_area": "Bedroom",
    "configuration_url": "http://192.168.1.57:8088/admin"
  },
  "origin": {"name": "pi-kiosk", "sw_version": "1.0.0", "support_url": "https://github.com/<owner>/pi-kiosk"},
  "has_entity_name": true,
  "qos": 1
}
```
**Player entities** (buttons, select video, text, position, title, duration, thumbnail, state) also add `{"topic": "pikiosk/kids_kiosk/player/status"}` to `availability` (mode `all`), so they show as unavailable when mpv is down. `hw_version` is read from `/proc/device-tree/model` plus the detected display type. `configuration_url` points at the read-only status page (section 10.6) and is left out if the UI is disabled.

### 7.4 Discovery payload examples (exact; shared fragments left out for brevity)

**Button – Play** · `homeassistant/button/pikiosk_kids_kiosk/play/config`
```json
{"name":"Play","unique_id":"pikiosk_kids_kiosk_play","default_entity_id":"button.kids_kiosk_play",
 "command_topic":"pikiosk/kids_kiosk/cmd/play","payload_press":"PRESS","icon":"mdi:play"}
```
Pause, Stop, Next and Previous are the same, using objects `pause`/`stop`/`next`/`previous`, icons `mdi:pause`/`mdi:stop`/`mdi:skip-next`/`mdi:skip-previous`, and their matching command topics.

**Select – Video** · `homeassistant/select/pikiosk_kids_kiosk/video/config`
```json
{"name":"Video","unique_id":"pikiosk_kids_kiosk_video","default_entity_id":"select.kids_kiosk_video",
 "command_topic":"pikiosk/kids_kiosk/cmd/select",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.selected }}",
 "options":["Baby Shark","Sesame Street Songs","Bath song","Calm lights (local)"],
 "icon":"mdi:playlist-play"}
```
Note: HA marks the select state invalid if `selected` is not in `options`. Publish `selected` only as a valid option. For ad-hoc plays, keep the previous value.

**Text – Play URL or ID** · `homeassistant/text/pikiosk_kids_kiosk/play_media/config`
```json
{"name":"Play URL or ID","unique_id":"pikiosk_kids_kiosk_play_media","default_entity_id":"text.kids_kiosk_play_media",
 "command_topic":"pikiosk/kids_kiosk/cmd/play_media",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.media_url or '' }}",
 "min":0,"max":255,"mode":"text","icon":"mdi:youtube"}
```

**Number – Volume** · `homeassistant/number/pikiosk_kids_kiosk/volume/config`
```json
{"name":"Volume","unique_id":"pikiosk_kids_kiosk_volume","default_entity_id":"number.kids_kiosk_volume",
 "command_topic":"pikiosk/kids_kiosk/cmd/volume",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.volume }}",
 "min":0,"max":80,"step":1,"mode":"slider","unit_of_measurement":"%","icon":"mdi:volume-high"}
```
(`max` = `playback.max_volume`.)

**Switch – Mute** · `homeassistant/switch/pikiosk_kids_kiosk/mute/config`
```json
{"name":"Mute","unique_id":"pikiosk_kids_kiosk_mute","default_entity_id":"switch.kids_kiosk_mute",
 "command_topic":"pikiosk/kids_kiosk/cmd/mute",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ 'ON' if value_json.muted else 'OFF' }}",
 "payload_on":"ON","payload_off":"OFF","state_on":"ON","state_off":"OFF","icon":"mdi:volume-off"}
```

**Light – Screen** (JSON schema) · `homeassistant/light/pikiosk_kids_kiosk/screen/config`
```json
{"name":"Screen","unique_id":"pikiosk_kids_kiosk_screen","default_entity_id":"light.kids_kiosk_screen",
 "schema":"json",
 "command_topic":"pikiosk/kids_kiosk/cmd/screen","state_topic":"pikiosk/kids_kiosk/screen/state",
 "brightness":true,"brightness_scale":255,"supported_color_modes":["brightness"],
 "icon":"mdi:tablet"}
```
State example: `{"state":"ON","brightness":178,"color_mode":"brightness"}`. When off: `{"state":"OFF","brightness":178,"color_mode":"brightness"}` (keep the last brightness so turning it on restores it).

**Number – Sleep timeout** · `homeassistant/number/pikiosk_kids_kiosk/sleep_timeout/config`
```json
{"name":"Sleep timeout","unique_id":"pikiosk_kids_kiosk_sleep_timeout","default_entity_id":"number.kids_kiosk_sleep_timeout",
 "command_topic":"pikiosk/kids_kiosk/cmd/sleep_timeout",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.sleep_timeout }}",
 "min":1,"max":240,"step":1,"mode":"box","unit_of_measurement":"min",
 "entity_category":"config","icon":"mdi:timer-sand"}
```

**Switch – Screen sleep** · `homeassistant/switch/pikiosk_kids_kiosk/sleep_enabled/config`
```json
{"name":"Screen sleep","unique_id":"pikiosk_kids_kiosk_sleep_enabled","default_entity_id":"switch.kids_kiosk_sleep_enabled",
 "command_topic":"pikiosk/kids_kiosk/cmd/sleep_enabled",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ 'ON' if value_json.sleep_enabled else 'OFF' }}",
 "entity_category":"config","icon":"mdi:sleep"}
```

**Select – After video** · `homeassistant/select/pikiosk_kids_kiosk/after_video/config`
```json
{"name":"After video","unique_id":"pikiosk_kids_kiosk_after_video","default_entity_id":"select.kids_kiosk_after_video",
 "command_topic":"pikiosk/kids_kiosk/cmd/after_video",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.after_video }}",
 "options":["loop","next","stop","blank"],"entity_category":"config","icon":"mdi:repeat"}
```

**Switch – Touch UI** (ON = UI shown/usable, OFF = locked) · `homeassistant/switch/pikiosk_kids_kiosk/ui_enabled/config`
```json
{"name":"Touch UI","unique_id":"pikiosk_kids_kiosk_ui_enabled","default_entity_id":"switch.kids_kiosk_ui_enabled",
 "command_topic":"pikiosk/kids_kiosk/cmd/ui_enabled",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ 'ON' if value_json.ui_enabled else 'OFF' }}",
 "icon":"mdi:gesture-tap-button"}
```

**Sensor – State** · `homeassistant/sensor/pikiosk_kids_kiosk/state/config`
```json
{"name":"State","unique_id":"pikiosk_kids_kiosk_state","default_entity_id":"sensor.kids_kiosk_state",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.state }}",
 "device_class":"enum","options":["idle","loading","playing","paused","error"],
 "json_attributes_topic":"pikiosk/kids_kiosk/state",
 "json_attributes_template":"{{ {'media_id': value_json.media_id, 'source': value_json.source, 'playlist_index': value_json.playlist_index, 'playlist_count': value_json.playlist_count, 'offline': value_json.offline, 'last_error': value_json.last_error} | tojson }}",
 "icon":"mdi:television-play"}
```

**Sensor – Title** · `…/sensor/pikiosk_kids_kiosk/title/config`
```json
{"name":"Title","unique_id":"pikiosk_kids_kiosk_title","default_entity_id":"sensor.kids_kiosk_title",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.title[:255] if value_json.title else '' }}",
 "icon":"mdi:format-title"}
```

**Sensor – Position** · `…/sensor/pikiosk_kids_kiosk/position/config`
```json
{"name":"Position","unique_id":"pikiosk_kids_kiosk_position","default_entity_id":"sensor.kids_kiosk_position",
 "state_topic":"pikiosk/kids_kiosk/state",
 "value_template":"{{ value_json.position | round(0) if value_json.position is not none else none }}",
 "device_class":"duration","unit_of_measurement":"s","icon":"mdi:timer-outline"}
```
**Sensor – Duration**: the same with object `duration` and `value_json.duration`.

**Sensor – Thumbnail URL** · `…/sensor/pikiosk_kids_kiosk/thumbnail/config`
```json
{"name":"Thumbnail URL","unique_id":"pikiosk_kids_kiosk_thumbnail","default_entity_id":"sensor.kids_kiosk_thumbnail",
 "state_topic":"pikiosk/kids_kiosk/state","value_template":"{{ value_json.thumbnail or '' }}",
 "icon":"mdi:image"}
```
Optional extra: an MQTT `image` entity (`url_topic` = `B/thumbnail_url`, a plain-string topic) for dashboards. Add it behind `mqtt.image_entity: true`.

**Sensor – CPU temperature** · `…/sensor/pikiosk_kids_kiosk/cpu_temp/config`
```json
{"name":"CPU temperature","unique_id":"pikiosk_kids_kiosk_cpu_temp","default_entity_id":"sensor.kids_kiosk_cpu_temp",
 "state_topic":"pikiosk/kids_kiosk/telemetry","value_template":"{{ value_json.cpu_temp }}",
 "device_class":"temperature","unit_of_measurement":"°C","state_class":"measurement",
 "suggested_display_precision":1,"entity_category":"diagnostic"}
```

**Sensor – Uptime** (as a boot timestamp, so HA shows "x hours ago" without a value every minute filling the recorder) · `…/sensor/pikiosk_kids_kiosk/uptime/config`
```json
{"name":"Uptime","unique_id":"pikiosk_kids_kiosk_uptime","default_entity_id":"sensor.kids_kiosk_uptime",
 "state_topic":"pikiosk/kids_kiosk/telemetry","value_template":"{{ value_json.boot_time }}",
 "device_class":"timestamp","entity_category":"diagnostic","icon":"mdi:clock-start"}
```

**Sensor – IP address** · `…/sensor/pikiosk_kids_kiosk/ip/config`
```json
{"name":"IP address","unique_id":"pikiosk_kids_kiosk_ip","default_entity_id":"sensor.kids_kiosk_ip",
 "state_topic":"pikiosk/kids_kiosk/telemetry","value_template":"{{ value_json.ip }}",
 "entity_category":"diagnostic","icon":"mdi:ip-network"}
```

**Diagnostics and config extras** (same pattern; include them in M2/M3):
- sensors `ytdlp_version` (diagnostic), `last_error` (diagnostic, from `B/state`) and `cache_used` (GB, diagnostic, M4)
- buttons `reload_config`, `update_ytdlp`, `sync_offline` (M4) and `restart` (all `entity_category: config`)

### 7.5 Full entity list (acceptance checklist)
| Entity id (default) | Component | Object | Command topic | State source |
|---|---|---|---|---|
| button.kids_kiosk_play | button | play | cmd/play | — |
| button.kids_kiosk_pause | button | pause | cmd/pause | — |
| button.kids_kiosk_stop | button | stop | cmd/stop | — |
| button.kids_kiosk_next | button | next | cmd/next | — |
| button.kids_kiosk_previous | button | previous | cmd/previous | — |
| select.kids_kiosk_video | select | video | cmd/select | state.selected |
| text.kids_kiosk_play_media | text | play_media | cmd/play_media | state.media_url |
| number.kids_kiosk_volume | number | volume | cmd/volume | state.volume |
| switch.kids_kiosk_mute | switch | mute | cmd/mute | state.muted |
| light.kids_kiosk_screen | light | screen | cmd/screen | screen/state |
| number.kids_kiosk_sleep_timeout | number | sleep_timeout | cmd/sleep_timeout | state.sleep_timeout |
| switch.kids_kiosk_sleep_enabled | switch | sleep_enabled | cmd/sleep_enabled | state.sleep_enabled |
| select.kids_kiosk_after_video | select | after_video | cmd/after_video | state.after_video |
| switch.kids_kiosk_ui_enabled | switch | ui_enabled | cmd/ui_enabled | state.ui_enabled |
| sensor.kids_kiosk_state | sensor | state | — | state.state |
| sensor.kids_kiosk_title | sensor | title | — | state.title |
| sensor.kids_kiosk_position | sensor | position | — | state.position |
| sensor.kids_kiosk_duration | sensor | duration | — | state.duration |
| sensor.kids_kiosk_thumbnail | sensor | thumbnail | — | state.thumbnail |
| sensor.kids_kiosk_cpu_temp | sensor | cpu_temp | — | telemetry.cpu_temp |
| sensor.kids_kiosk_uptime | sensor | uptime | — | telemetry.boot_time |
| sensor.kids_kiosk_ip | sensor | ip | — | telemetry.ip |

### 7.6 Manual test commands (for the README)
```bash
mosquitto_sub -h $BROKER -u $U -P $P -v -t 'pikiosk/kids_kiosk/#' -t 'homeassistant/+/pikiosk_kids_kiosk/#'
mosquitto_pub -h $BROKER -u $U -P $P -t pikiosk/kids_kiosk/cmd/play_media -m 'https://youtu.be/XqZsoesa55w'
mosquitto_pub -h $BROKER -u $U -P $P -t pikiosk/kids_kiosk/cmd/screen -m '{"state":"ON","brightness":60}'
mosquitto_pub -h $BROKER -u $U -P $P -t pikiosk/kids_kiosk/cmd/json -m '{"command":"seek","args":{"position":30}}'
```

---

## 8. Home Assistant YAML (ready to paste)

Deliver this as `ha/packages/pikiosk.yaml`. The user enables packages (`homeassistant: packages: !include_dir_named packages`) and drops the file in, or pastes the sections into `configuration.yaml`. Entity ids assume `device.id: kids_kiosk`. The README tells users to search-and-replace `kids_kiosk` if they chose another id.

```yaml
# ha/packages/pikiosk.yaml
template:
  - sensor:
      - name: "Kids Kiosk volume level"
        unique_id: pikiosk_kids_kiosk_volume_level
        state: >
          {% set v = states('number.kids_kiosk_volume') %}
          {{ (v | float(0) / 100) | round(2) if v not in ['unknown','unavailable'] else none }}

media_player:
  - platform: universal
    name: Kids Kiosk
    unique_id: pikiosk_kids_kiosk_media_player
    device_class: tv
    state_template: >
      {% set avail = not is_state('sensor.kids_kiosk_state', 'unavailable') %}
      {% set s = states('sensor.kids_kiosk_state') %}
      {% if not avail %}off
      {% elif is_state('light.kids_kiosk_screen', 'off') and s not in ['playing'] %}off
      {% elif s in ['playing', 'paused'] %}{{ s }}
      {% elif s == 'loading' %}buffering
      {% else %}idle{% endif %}
    attributes:
      volume_level: sensor.kids_kiosk_volume_level
      is_volume_muted: switch.kids_kiosk_mute
      media_title: sensor.kids_kiosk_title
      media_duration: sensor.kids_kiosk_duration
      media_position: sensor.kids_kiosk_position
      media_position_updated_at: sensor.kids_kiosk_position|last_changed
      entity_picture: sensor.kids_kiosk_thumbnail
      source: select.kids_kiosk_video
      source_list: select.kids_kiosk_video|options
    commands:
      turn_on:
        action: light.turn_on
        target: { entity_id: light.kids_kiosk_screen }
      turn_off:
        - action: button.press
          target: { entity_id: button.kids_kiosk_stop }
        - action: light.turn_off
          target: { entity_id: light.kids_kiosk_screen }
      media_play:
        action: button.press
        target: { entity_id: button.kids_kiosk_play }
      media_pause:
        action: button.press
        target: { entity_id: button.kids_kiosk_pause }
      media_play_pause:
        action: mqtt.publish
        data: { topic: pikiosk/kids_kiosk/cmd/play_pause, payload: PRESS }
      media_stop:
        action: button.press
        target: { entity_id: button.kids_kiosk_stop }
      media_next_track:
        action: button.press
        target: { entity_id: button.kids_kiosk_next }
      media_previous_track:
        action: button.press
        target: { entity_id: button.kids_kiosk_previous }
      volume_set:
        action: number.set_value
        target: { entity_id: number.kids_kiosk_volume }
        data:
          value: "{{ (volume_level * 100) | round(0) | int }}"
      volume_up:
        action: number.set_value
        target: { entity_id: number.kids_kiosk_volume }
        data:
          value: "{{ [states('number.kids_kiosk_volume') | int(0) + 5, state_attr('number.kids_kiosk_volume','max') | int(80)] | min }}"
      volume_down:
        action: number.set_value
        target: { entity_id: number.kids_kiosk_volume }
        data:
          value: "{{ [states('number.kids_kiosk_volume') | int(0) - 5, 0] | max }}"
      volume_mute:
        action: "switch.turn_{{ 'on' if is_volume_muted else 'off' }}"
        target: { entity_id: switch.kids_kiosk_mute }
      select_source:
        action: select.select_option
        target: { entity_id: select.kids_kiosk_video }
        data: { option: "{{ source }}" }
      play_media:
        action: text.set_value
        target: { entity_id: text.kids_kiosk_play_media }
        data: { value: "{{ media_content_id }}" }
      media_seek:
        action: mqtt.publish
        data: { topic: pikiosk/kids_kiosk/cmd/seek, payload: "{{ seek_position }}" }

script:
  kids_kiosk_play_video:
    alias: "Kids Kiosk: play video"
    mode: restart
    fields:
      ref:
        description: "YouTube URL, 11-char video ID, or approved item name"
        example: "https://youtu.be/XqZsoesa55w"
        required: true
        selector: { text: {} }
      volume:
        description: "Optional volume 0-100"
        selector: { number: { min: 0, max: 80 } }
    sequence:
      - action: light.turn_on
        target: { entity_id: light.kids_kiosk_screen }
      - if: "{{ volume is defined }}"
        then:
          - action: number.set_value
            target: { entity_id: number.kids_kiosk_volume }
            data: { value: "{{ volume }}" }
      - action: mqtt.publish
        data:
          topic: pikiosk/kids_kiosk/cmd/play_media
          payload: "{{ {'ref': ref} | tojson }}"

automation:
  - id: pikiosk_bedtime_off
    alias: "Kids Kiosk: bedtime off"
    triggers:
      - trigger: time
        at: "19:00:00"
    actions:
      - action: button.press
        target: { entity_id: button.kids_kiosk_stop }
      - action: switch.turn_off            # lock the touch UI overnight
        target: { entity_id: switch.kids_kiosk_ui_enabled }
      - action: light.turn_off
        target: { entity_id: light.kids_kiosk_screen }
      # Optional gentle wind-down instead of a hard stop:
      # - action: number.set_value  (lower volume in steps) … then stop

  - id: pikiosk_morning_on
    alias: "Kids Kiosk: morning on"
    triggers:
      - trigger: time
        at: "07:00:00"
    conditions:
      - condition: time
        weekday: [mon, tue, wed, thu, fri, sat, sun]
    actions:
      - action: switch.turn_on
        target: { entity_id: switch.kids_kiosk_ui_enabled }
      - action: number.set_value
        target: { entity_id: number.kids_kiosk_volume }
        data: { value: 30 }
      - action: light.turn_on
        target: { entity_id: light.kids_kiosk_screen }
        data: { brightness_pct: 60 }

  - id: pikiosk_play_specific_video
    alias: "Kids Kiosk: play favourite after school"
    triggers:
      - trigger: state
        entity_id: input_boolean.after_school   # example trigger; change to suit
        to: "on"
    actions:
      - action: script.kids_kiosk_play_video
        data:
          ref: "Baby Shark"        # approved item name, URL, or ID
          volume: 35

  - id: pikiosk_offline_alert
    alias: "Kids Kiosk: notify if offline 10 min"
    triggers:
      - trigger: state
        entity_id: sensor.kids_kiosk_state
        to: unavailable
        for: "00:10:00"
    actions:
      - action: persistent_notification.create
        data:
          title: "Kids Kiosk offline"
          message: "The kiosk has been unavailable for 10 minutes."
```

Implementation notes for the HA YAML:
- Validate the YAML against the HA version in use (Q1). It uses the modern `triggers:`/`actions:`/`action:` syntax (HA ≥ 2024.10). The README includes a legacy (`service:`/`platform:`) variant.
- `universal` attribute overrides accept `entity_id` or `entity_id|attribute` (used here for `source_list` and `media_position_updated_at`).
- Also ship `ha/dashboards/pikiosk-card.yaml`: a `media-control` card for the universal player, plus an `entities` card with the select, text, volume, screen light, sleep switch and timeout, after-video, touch UI, and the diagnostics.

---

## 9. Screen control

### 9.1 Backlight discovery (`screen/backlight.py`)
1. If `screen.backlight_device` isn't `auto`, use `/sys/class/backlight/<name>`.
2. Otherwise, list `/sys/class/backlight/*`. Prefer, in order: an entry whose `device` symlink resolves under a DSI or I2C path (for example `…/10-0045` for the original 7" display, or the TD2 panel's backlight node), then names containing `rpi_backlight` or `dsi`, then the only entry present. Log the choice, along with `max_brightness` and the detected display type.
3. If nothing is found, run in **no-backlight mode**. The light entity still exists and maps to the output on/off method only, and a warning event is published.
4. Read `max_brightness` once (it varies: often 255 on the original 7" and a small range such as 31 on some TD2 kernels; never assume).

### 9.2 Mapping
- HA brightness `b ∈ 1..255` maps to sysfs `max(round(b/255*max), ceil(min_brightness% * max), 1)`. The reverse uses `actual_brightness` when available, otherwise the last written value.
- **Extra dim** (`switch.<id>_extra_dim`, persisted, published as `extra_dim: ON|OFF` in the screen state) goes below the backlight's lowest step. While on, the backlight is held at sysfs 1 and the brightness number and the light's brightness set `dim_level` (1–100 %, persisted separately from the normal brightness) instead. The picture is darkened by mpv `contrast` gain (black stays black) and a black overlay on the touch grid, with light fraction `0.1 + 0.9·(level−1)/99` and gain `light^(1/2.2)`; the screen state's `brightness` reports the dim level. Off restores the normal brightness and an untouched picture.
- **Grayscale** (`switch.<id>_grayscale`, persisted, published as `grayscale` in the player state) sets mpv `saturation` to −100 (documented grayscale) while on and 0 while off, and applies a CSS `filter: grayscale(1)` on the touch grid. Re-applied when mpv reconnects.
- **ON:** write `bl_power=0`, then `brightness=<mapped>`. With `blank_method` set to `output` or `both`, also run `wlr-randr --output <name> --on` (plus the transform if needed) **first**.
- **OFF:** write `bl_power=4` (and optionally `brightness=0`). With `output` or `both`, also run `wlr-randr --output <name> --off`.
- **Default `blank_method: backlight`.** Turning the Wayland output off can make the compositor re-layout windows and confuse Chromium or mpv sizes. Backlight-off is instant and keeps the windows intact. Keep `output` as an option for maximum power saving, and test it on both displays during M2.
- Permissions: the installer adds udev rule `90-pikiosk-backlight.rules`:
  ```
  SUBSYSTEM=="backlight", ACTION=="add", RUN+="/bin/chgrp video /sys%p/brightness /sys%p/bl_power", RUN+="/bin/chmod g+w /sys%p/brightness /sys%p/bl_power"
  ```
  and puts user `kiosk` in the `video`, `input` and `render` groups. The service must never need root at runtime.
- Writes are serialised through one asyncio lock. The service reads back and reconciles every 60 s (another process or the desktop's own blanking could change it), and publishes on drift.

### 9.3 Disable competing blanking
- The installer turns off Raspberry Pi OS screen blanking (`raspi-config nonint do_blanking 1`, which disables it) and removes or stops any `swayidle` entry from labwc autostart. `pikioskd` is the **only** owner of blanking.

### 9.4 Acceptance behaviour
- Light OFF → the panel is visibly dark within 500 ms. Light ON → the previous brightness comes back within 500 ms.
- With the screen off: playback keeps running if it was playing (bedtime automations stop it explicitly), and touch wakes the screen if `wake_on_touch` is set (section 6.5).

---

## 10. Local touch UI and lockdown

### 10.1 Recommendation: Chromium kiosk + local web UI (served by `pikioskd`), with mpv as a separate always-on-top window
Reasons:
- Chromium on Wayland handles big thumbnail grids, touch scrolling, fonts and images well, with no native toolkit work. The UI is plain HTML, CSS and JS with no build step, and is easy for parents to restyle.
- The video path stays in mpv, which is lighter, needs no YouTube web player, and is fully controllable. The UI never embeds YouTube, so there are no recommendations or ads, and nothing to click through to.
- The same HTTP server serves cached thumbnails, a status page and the WebSocket state, so it adds little complexity.
- Alternative considered: a native overlay (mpv Lua/OSC or pygame/SDL). It's lighter on RAM, but grid rendering, image loading and touch handling would all be custom. Keep it as a fallback (`ui.mode: native`, post-v1) for Pi models with less memory.
- `ui.enabled: false` → no Chromium. Idle shows a black screen (labwc background set to black), and control is HA only.

### 10.2 Window and session management
- `pikiosk-ui.service` runs:
  ```
  chromium --kiosk --ozone-platform=wayland --app=http://127.0.0.1:8088/ \
    --noerrdialogs --disable-infobars --no-first-run --incognito \
    --disable-pinch --overscroll-history-navigation=0 \
    --disable-features=Translate,TouchpadOverscrollHistoryNavigation,MediaRouter \
    --disable-session-crashed-bubble --check-for-update-interval=31536000 \
    --autoplay-policy=no-user-gesture-required --password-store=basic \
    --user-data-dir=%h/.local/share/pi-kiosk/chromium
  ```
  (The binary is `chromium` or `chromium-browser` depending on the image, so the installer detects it.)
- On Wayland the window stacking is set with **labwc window rules** (section 13.5): mpv's `app_id` (`mpv`) → fullscreen, always-on-top, no decorations. Chromium → fullscreen.
- Since mpv runs with `force-window=no`, its window appears only while media is loaded. On stop or EOF the window closes and the grid shows underneath, with no focus juggling. The WebSocket pushes the state change so the grid refreshes its "now playing" highlight.

### 10.3 Touch handling
- **During playback** (mpv window on top), mpv receives the touch as a mouse event. `input.conf` maps:
  ```
  MBTN_LEFT      script-message pikiosk-touch tap
  MBTN_LEFT_DBL  script-message pikiosk-touch double
  MBTN_RIGHT     ignore
  WHEEL_UP       ignore
  WHEEL_DOWN     ignore
  ```
  mpv sends these to IPC clients as `client-message` events, and the controller applies `ui.touch_during_playback`:
  - `ignore`: the default. It only resets the idle timer. Recommended, because random taps from the child shouldn't stop the video.
  - `pause_toggle`
  - `stop_to_grid`
- Long press (`ui.long_press_stop_s > 0`) is detected by `InputMonitor` through evdev (press, then release time), not by mpv.
- **When locked or asleep:** `InputMonitor` grabs the touch device (`EVIOCGRAB`), so no touch reaches Chromium or mpv. Touches still get counted (for wake and idle reset) and logged at DEBUG.
- Find the touch device by capability (`ABS_MT_POSITION_X` and `INPUT_PROP_DIRECT`). Config override: `input.touch_device: /dev/input/by-path/...`. Handle hot-unplug by retrying every 5 s.

### 10.4 Grid UI (`web/static`)
- One page with a full-screen grid of **large thumbnails** (`ui.columns`, default 3 in landscape and 2 in portrait), high contrast, no text unless `show_titles`. Use a large touch target (≥ 200 px tall) and rounded corners.
- Approved catalog items only. Playlists show their first video's thumbnail with a small "stack" badge.
- Tap → `POST /api/play {"name": "<catalog name>"}`. Show visual feedback (scale and highlight) for 300 ms, and debounce: ignore further taps for 2 s after a play request, because the child may tap repeatedly.
- No scrolling beyond the grid, no text selection (`user-select:none`), no context menu (`contextmenu` prevented), no zoom (`touch-action: manipulation`, viewport `user-scalable=no`), no links, no forms.
- Offline-friendly: all assets are local. Thumbnails come through `/thumbs/<id>.jpg` (proxied and cached on disk on first fetch, or created by CacheMgr).
- The WebSocket `/ws` streams `KioskState` (subset), so the UI shows the locked screen and the "now playing" glow, and reloads itself when the catalog changes.
- If the WebSocket drops, retry every 2 s with an unobtrusive state (no error dialogs). If the server is unreachable at startup, show black and retry.

### 10.5 Parent lock
- The HA switch `Touch UI` is ON → the grid works. OFF → locked: the grid is replaced by `ui.locked_screen` (black, logo or a calm clock) and the touch device is grabbed. HA controls (play and so on) still work while locked, and videos started from HA play normally.
- Optional on-device unlock (`ui.parent_unlock.enabled`): hold `hold_corner` for `hold_s` → a PIN pad (digits only, big buttons). A correct PIN unlocks for 15 minutes (or until HA locks it again). Three wrong attempts → a 5-minute cooldown. The PIN is verified **server-side** (`POST /api/unlock`). While locked the corner hold needs the evdev grab released for that region, so implement it in `InputMonitor` (corner-hold detection on raw coordinates, mapped through the rotation), which then tells the UI to show the PIN pad and releases the grab.
- Lock state persists across reboots (section 12.3).

### 10.6 HTTP API (`web/server.py`; binds `127.0.0.1` by default)
| Method | Path | Notes |
|---|---|---|
| GET | `/` | Grid UI |
| GET | `/ws` | WebSocket state stream |
| GET | `/api/catalog` | `[{"name","type","thumb","index"}]` |
| GET | `/api/state` | Current `KioskState` (JSON) |
| POST | `/api/play` | `{"name"}`, catalog only. Rejected (403) when locked |
| POST | `/api/stop` | Rejected when locked |
| POST | `/api/unlock` | `{"pin"}` |
| GET | `/thumbs/{key}.jpg` | Cached thumbnails |
| GET | `/admin` | **Read-only** status page (version, state, logs tail, cache status). Only served when `ui.bind` isn't loopback **and** `ui.admin_enabled: true`. No control actions without HA. |
| GET | `/healthz` | `200 {"mqtt":true,"mpv":true}`, or `503` |

Security: the UI port binds to loopback by default. If it's exposed to the LAN for `/admin` or thumbnails used by HA, every mutating endpoint still accepts loopback only.

---

## 11. Offline cache and yt-dlp

### 11.1 yt-dlp installation
- Install in the venv: `pip install -U "yt-dlp[default]"` (plus `yt-dlp-ejs`, if separate in the current release). Point mpv at it: `script-opts=ytdl_hook-ytdl_path=/opt/pi-kiosk/venv/bin/yt-dlp` in `mpv.conf`.
- `ffmpeg` (apt) for merging separate video and audio.

### 11.2 Update timer
- `pikiosk-ytdlp-update.timer` runs daily at a random time between 03:00 and 05:00 (`OnCalendar=*-*-* 03:00:00`, `RandomizedDelaySec=2h`, `Persistent=true`), and also 5 minutes after boot (`OnBootSec=5min`).
- The service runs `/opt/pi-kiosk/bin/update-ytdlp.sh`:
  1. `venv/bin/pip install -U "yt-dlp[default]"`, or `--pre` / a nightly source when `update_channel: nightly`.
  2. Smoke test: `yt-dlp --simulate --quiet <known-good test id from config or default>`.
  3. If the smoke test fails, roll back to the previous version (`pip install yt-dlp==<old>`) and log an error.
  4. Write the version to `~/.local/state/pi-kiosk/ytdlp_version`. `pikioskd` publishes it.
- The HA button `update_ytdlp` triggers `systemctl --user start pikiosk-ytdlp-update.service`.
- **Auto-heal:** if 3 consecutive YouTube loads fail with yt-dlp extractor errors, trigger an update straight away (at most once per hour).

### 11.3 Offline mode (`cache/cache_manager.py`)
- When `offline.enabled`, sync every `sync_interval_h`, only inside `download_window`, and never while playing a streamed video (to save bandwidth for it):
  - Expand catalog playlists (`yt-dlp --flat-playlist -J`) up to `max_items`.
  - Download missing items with `-f <ytdl_format> --merge-output-format mkv -o '<dir>/%(id)s.%(ext)s' --download-archive <dir>/archive.txt --write-thumbnail --convert-thumbnails jpg --write-info-json --no-playlist --restrict-filenames` (plus `--limit-rate` and `extra_args`).
  - Write the info JSON (title, duration) to an index `cache/index.json` (id → file, title, duration, thumbnail path, size, last_played, downloaded_at).
  - Evict by LRU (`last_played`, then `downloaded_at`) when over `max_size_gb`. **Never** evict items in the current catalog unless the space is truly insufficient, and then log a warning.
  - Delete files for items no longer in the catalog after 7 days (configurable).
- Playback prefers the cache when `prefer_offline` is set and the file exists and is complete (ignore `.part` files). With `only_offline: true`, uncached items are greyed out in the UI and rejected from HA with `error: not_cached`.
- Publish progress events (`{"type":"cache","data":{"id","progress":0.42}}`, throttled) and the `cache_used` sensor.
- Run downloads at low priority (`nice -n 10`, `ionice -c3`), one at a time.

### 11.4 Known YouTube fragility (design for it)
- Recent yt-dlp releases need an **external JavaScript runtime** (for example Deno) for full YouTube support. The installer must install the runtime that the current yt-dlp docs recommend (check during implementation; Deno ships aarch64 builds) and keep it updated in the same timer.
- Optional `ytdlp.cookies_file` for "Sign in to confirm you're not a bot" blocks. It's off by default and documented in the README.
- Errors from yt-dlp are classified (`network`, `unavailable`, `age_restricted`, `geo`, `bot_check`, `extractor`) by matching stderr patterns, then published in `last_error` and events. An `extractor` error counts towards auto-heal.

---

## 12. Reliability

### 12.1 Supervision and watchdog
- `pikioskd.service`: `Type=notify`, `WatchdogSec=30`, `Restart=always`, `RestartSec=3`, `StartLimitIntervalSec=0`.
- The service sends `READY=1` after config is loaded and the event loop is running (not after MQTT connects, so it doesn't block startup when the network is down). It sends `WATCHDOG=1` every 10 s **only if** the health check passes: the event loop is responsive (a heartbeat task within 2 s), and **no** internal task has died (tasks are supervised; an unexpected exception in a core task → log, then exit non-zero so systemd restarts us). It sends `STATUS=` with a one-line summary (`mqtt=up mpv=up state=playing`).
- MQTT or mpv being disconnected is **not** a watchdog failure (we reconnect ourselves).
- `pikiosk-mpv.service`: `Restart=always`, `RestartSec=2`. If mpv is stuck (an IPC request times out 3 times in a row while the socket stays connected), `pikioskd` restarts it with `systemctl --user restart pikiosk-mpv`, at most once per minute.
- `pikiosk-ui.service`: `Restart=always`. Also check Chromium liveness: if `/api/state` hasn't been polled by the UI's heartbeat (sent over the WS every 30 s) for 2 minutes while `ui.enabled`, restart the UI unit.
- Optional hardware watchdog (`install.sh --hw-watchdog`): set `RuntimeWatchdogSec=15` in `/etc/systemd/system.conf.d/pikiosk.conf`, so the Pi reboots if the kernel or systemd hangs.

### 12.2 MQTT reconnect
- Exponential backoff with jitter, from `min_delay_s` to `max_delay_s`. Set the LWT on every connect. After connecting: subscribe to `B/cmd/#` and `D/status`, publish discovery, publish all state (screen, state, telemetry), then publish `B/status online` and `B/player/status`.
- Outgoing state during a disconnect: keep only the **latest** per topic (coalescing), and flush it on reconnect.
- Commands that arrive while mpv is down: `play`/`play_media`/`select` are queued (only the latest, valid for 30 s) and run once mpv reconnects. Other commands apply to state (volume and so on) and are re-applied on reconnect.

### 12.3 Persistence (`persistence.py`)
- Persisted fields: `volume`, `muted`, `brightness`, `screen_on`, `sleep_enabled`, `sleep_timeout_min`, `after_video`, `ui_locked`, `selected`, `last_item` (ref and position, updated every 15 s while playing and on stop), `resume_positions` (per video id, approved items only, capped at 200 entries), `published_discovery_topics`, and `schema_version`.
- Writes are atomic (write to a temp file, `fsync`, then `rename`), and debounced (at most every 2 s, and always on shutdown).
- On boot: load state (on corruption, move the file to `state.json.bad` and use defaults). Config defaults apply first, then persisted values on top. Apply volume and mute to mpv, and brightness and screen to the backlight. If `resume_on_boot` is set, load `last_item` at its position, **paused** if the screen was off, otherwise playing. Otherwise stay idle.
- Bump `schema_version` and migrate on load.

### 12.4 Logging
- Use the stdlib `logging` to stdout. journald captures it (`SyslogIdentifier=pikioskd`). The format is `level logger message key=value…`, or JSON when `logging.json`.
- Log levels: commands received at INFO (source plus command, with secrets redacted), state transitions at INFO, IPC traffic at DEBUG, and errors with context.
- Never log the MQTT password, cookies or the PIN.
- `journald` limits: the installer sets `SystemMaxUse=200M` in a drop-in, to protect the SD card.

### 12.5 SD-card friendliness (optional, recommended)
- Use journald `Storage=volatile`, or keep it persistent with a size cap. Set `noatime`. Write `state.json` only when it changes (debounced). The cache directory can live on NVMe or USB.

---

## 13. Installation, OS setup and systemd

### 13.1 `install.sh` (idempotent; run as root via `sudo ./install.sh [options]`)
Options: `--user kiosk`, `--display auto|td2|legacy7`, `--rotation 0|90|180|270`, `--no-ui`, `--hw-watchdog`, `--offline`, `--dev` (editable install from the checkout).

Steps:
1. Preflight: check it's a Pi 5 (`/proc/device-tree/model`), Bookworm (`/etc/os-release`), 64-bit, and labwc present. Warn if not, and abort unless `--force`.
2. apt: `mpv ffmpeg python3-venv python3-dev wlr-randr jq mosquitto-clients chromium (or chromium-browser) libevdev2 fonts-noto-color-emoji curl unzip`.
3. Create user `kiosk` if missing. Add it to the groups `video input render audio`. Enable auto-login to the desktop (`raspi-config nonint do_boot_behaviour B4`) and labwc (`raspi-config nonint do_wayland W3`, if applicable). Run `loginctl enable-linger kiosk`.
4. Copy the app to `/opt/pi-kiosk` and create a venv. Install the package plus `requirements.lock` and yt-dlp. Install the JS runtime for yt-dlp (section 11.4).
5. Set up `/etc/pi-kiosk/` with the example config and secrets, if they're missing (**never overwrite** existing files). Permissions are as in section 5.1.
6. Install the udev rule (9.2) and reload udev. Install the invisible cursor theme.
7. Install the mpv config to `~kiosk/.config/mpv/` (or point to it with `--config-dir` in the unit).
8. labwc: write `~kiosk/.config/labwc/autostart` (**replacing** the default desktop session items: no `wf-panel-pi`, no `pcmanfm --desktop`, no `squeekboard`/on-screen keyboard, no `swayidle`), merge `rc.xml.fragment` into `~kiosk/.config/labwc/rc.xml` (keeping a backup), and write `environment` (`XCURSOR_THEME=pikiosk-invisible`, `XCURSOR_SIZE=24`). Disable `squeekboard` system-wide if it's auto-launched.
9. Disable screen blanking (`raspi-config nonint do_blanking 1`), and disable notifications and update popups for the kiosk user.
10. Install the systemd user units into `/etc/systemd/user/` (or `~kiosk/.config/systemd/user/`) and enable them for the user: `pikioskd.service`, `pikiosk-mpv.service`, `pikiosk-ui.service` (unless `--no-ui`) and `pikiosk-ytdlp-update.timer`.
11. Optional: the hardware watchdog drop-in, and the journald size cap.
12. Print a summary: detected display, backlight device, output name, next steps (edit config and secrets), and how to view logs.
13. Validate the config (`pikioskd --check-config`) and print the result.

`uninstall.sh` reverses steps 4, 6, 8 and 10 (restoring backups). It keeps `/etc/pi-kiosk` unless `--purge`.

### 13.2 Session wiring
- `pikioskd.service` is `WantedBy=default.target` (user manager, started at boot thanks to linger), so MQTT is up even without the GUI.
- mpv and Chromium need the Wayland environment. labwc's `autostart` runs:
  ```sh
  dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_RUNTIME_DIR &&
  systemctl --user start pikiosk-session.target &
  ```
  `pikiosk-session.target` `Wants=pikiosk-mpv.service pikiosk-ui.service`, and both units are `PartOf=pikiosk-session.target`. If the compositor restarts, autostart runs again and the target is started again.

### 13.3 Unit files (exact starting point)

`pikioskd.service`
```ini
[Unit]
Description=Pi Video Kiosk controller (MQTT, player control, screen)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=notify
NotifyAccess=main
ExecStart=/opt/pi-kiosk/venv/bin/pikioskd --config /etc/pi-kiosk/config.yaml
Restart=always
RestartSec=3
WatchdogSec=30
TimeoutStopSec=10
Environment=PYTHONUNBUFFERED=1
SyslogIdentifier=pikioskd
# Hardening (user unit; keep compatible)
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
```

`pikiosk-mpv.service`
```ini
[Unit]
Description=Pi Video Kiosk mpv player
PartOf=pikiosk-session.target
After=pikiosk-session.target

[Service]
Type=simple
RuntimeDirectory=pi-kiosk
RuntimeDirectoryPreserve=yes
ExecStart=/usr/bin/mpv --idle=yes --force-window=no \
  --config-dir=%h/.config/mpv \
  --input-ipc-server=%t/pi-kiosk/mpv.sock
Restart=always
RestartSec=2
SyslogIdentifier=pikiosk-mpv

[Install]
WantedBy=pikiosk-session.target
```

`pikiosk-ui.service`
```ini
[Unit]
Description=Pi Video Kiosk touch UI (Chromium kiosk)
PartOf=pikiosk-session.target
After=pikiosk-session.target pikioskd.service

[Service]
Type=simple
ExecStartPre=/opt/pi-kiosk/bin/wait-for-http http://127.0.0.1:8088/healthz 60
ExecStart=/opt/pi-kiosk/bin/run-chromium.sh
Restart=always
RestartSec=3
SyslogIdentifier=pikiosk-ui

[Install]
WantedBy=pikiosk-session.target
```
(`run-chromium.sh` detects the binary, clears the "crashed" flags in Preferences, and execs with the flags from section 10.2.)

`pikiosk-session.target`
```ini
[Unit]
Description=Pi Video Kiosk graphical session components
Wants=pikiosk-mpv.service pikiosk-ui.service
```

`pikiosk-ytdlp-update.service` / `.timer`
```ini
# .service
[Unit]
Description=Update yt-dlp for Pi Video Kiosk
[Service]
Type=oneshot
Nice=10
ExecStart=/opt/pi-kiosk/bin/update-ytdlp.sh
SyslogIdentifier=pikiosk-ytdlp-update

# .timer
[Unit]
Description=Daily yt-dlp update
[Timer]
OnBootSec=5min
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=2h
Persistent=true
[Install]
WantedBy=timers.target
```
(The `pip install` in the venv needs write access, so `/opt/pi-kiosk/venv` is owned by `kiosk`.)

### 13.4 mpv config (`system/mpv/mpv.conf`)
```ini
# Locked-down kiosk playback
fullscreen=yes
keep-open=no
idle=yes
force-window=no
osc=no
osd-level=0
osd-bar=no
border=no
cursor-autohide=always
input-default-bindings=no
input-vo-keyboard=no
input-builtin-bindings=no
stop-screensaver=yes
hwdec=auto-safe
vo=gpu
gpu-context=wayland
profile=fast
cache=yes
demuxer-max-bytes=150MiB
demuxer-readahead-secs=60
volume-max=100
ytdl=yes
ytdl-format=bestvideo[height<=?720][vcodec^=avc1][fps<=?30]+bestaudio[acodec^=mp4a]/bestvideo[height<=?720][fps<=?30]+bestaudio/best[height<=?720]/best
script-opts=ytdl_hook-ytdl_path=/opt/pi-kiosk/venv/bin/yt-dlp
ytdl-raw-options=no-playlist=
sub-auto=no
sid=no
msg-level=all=info
```
`pikioskd` overrides the format and audio device per load from config (`set_property`). `input.conf` contains **only** the bindings from section 10.3 (with `input-default-bindings=no`, nothing else works, so no `q` quits and no `f` exits fullscreen).

### 13.5 labwc `rc.xml` fragment
```xml
<windowRules>
  <windowRule identifier="mpv" serverDecoration="no">
    <action name="ToggleFullscreen"/>
    <action name="ToggleAlwaysOnTop"/>
    <action name="Focus"/>
  </windowRule>
  <windowRule identifier="chromium*" serverDecoration="no">
    <action name="ToggleFullscreen"/>
  </windowRule>
</windowRules>
<touch deviceName="" mapToOutput="DSI-1"/>   <!-- installer fills real values -->
<keyboard><!-- remove default keybinds that could open menus/terminals --></keyboard>
<mouse><!-- no root-menu on desktop click --></mouse>
```
Important: check the exact window-rule syntax (`identifier` matches `app_id`; for actions on map, the attribute names and whether `ToggleFullscreen` vs mpv's own `fullscreen=yes` is enough) against the labwc version shipped. mpv's `fullscreen=yes` alone may be enough, and the rule then only needs always-on-top. The installer must also strip the default labwc root menu and the keybindings (for example `A-F4`, `W-…`) so that if a keyboard is ever plugged in it can't reach a terminal.

---

## 14. Testing strategy

### 14.1 Fakes
- **FakeMpvServer** (`tests/fakes/fake_mpv_server.py`, also `tools/fake_mpv.py`): an asyncio unix-socket server that implements the subset of JSON IPC we use:
  - `get_property` / `set_property` for pause, volume, mute, loop-file, time-pos, duration and media-title
  - `observe_property` (emits `property-change`)
  - `loadfile` (emits `start-file` → `file-loaded` and sets the title from a test registry)
  - `stop` (→ `end-file reason=stop`)
  - `playlist-next/prev`, `seek`, `script-message` injection (→ `client-message`)
  It also offers controls such as `simulate_eof()`, `simulate_error()`, `drop_connection()` and `hang()` (stop replying).
- **FakeMqtt**: an in-memory `MqttTransport` that records publishes (topic, payload, retain, qos), lets tests inject messages (with retain flag), and simulates disconnect and reconnect, including LWT delivery.
- **FakeBacklight**: a tmp directory laid out like `/sys/class/backlight/x/{brightness,max_brightness,bl_power,actual_brightness}`, with parameterised `max_brightness` values (255, 31).
- **FakeClock**: controllable monotonic time for the sleep and dim timers.
- **FakeTouch**: injects press and release events.
- **FakeYtDlp**: a stub script on PATH that returns canned JSON and errors.

### 14.2 Required tests (minimum)
- Config: valid example loads. Each invalid case gives a readable error. `!secret` resolution works. Env overrides apply.
- Catalog: the URL parser table (≥ 25 cases, including `t=1m30s`, shorts, `si=` stripping, a playlist-only URL, `ytsearch:` rejected, a non-allowed host rejected, `file:` outside the allowed directories rejected).
- Discovery: **golden snapshot** JSON for every entity, matching section 7.4. Topic builder output. Empty payloads for removed entities.
- Bridge: each command topic maps to the right controller command. Retained commands are ignored. The HA birth triggers a republish. LWT and `online` ordering. Position throttling.
- Player: loadfile on play_media. after_video in all four modes, for single videos and playlists. An error triggers retry, then cache fallback, then idle. Reconnect re-applies volume and mute. A hang triggers the restart request.
- Screen: brightness mapping for max 255 and 31. min_brightness floor. Off and on restore the brightness. The sleep timer fires only when allowed. Resets on touch, command and playback. Dim-before-sleep. The wake-touch is swallowed (grab then release).
- Persistence: atomic write, corrupt-file recovery, restore after a simulated restart (new app instance, same state dir), and the `resume_on_boot` path.
- Web API: catalog only. Play rejected when locked. PIN cooldown.
- End-to-end (FakeMqtt + FakeMpv + FakeBacklight): "HA select → video plays → EOF → after_video=blank → screen off → HA light on → screen restored".
- Optional integration (`-m mosquitto`): a real mosquitto in Docker or local, plus a discovery round-trip.
- Target coverage: ≥ 85 % on `pikiosk/` excluding `simulate.py` and `web/static`.

### 14.3 Simulation and dev
- `pikioskd --simulate` runs with fakes for mpv (or a real local mpv if present), the backlight (tmp sysfs) and touch, against a real broker, so the whole HA experience can be built on a laptop.
- `make test`, `make lint` (ruff + mypy), and `make simulate`.
- CI (GitHub Actions): lint and test on Python 3.11 on ubuntu-latest, plus an arm64 job (qemu or native runner) for a smoke import.

### 14.4 On-device manual test script (`docs/TESTPLAN.md`)
A checklist that mirrors the acceptance criteria, including power-pull tests (pull PoE mid-playback, then confirm it boots back to a correct state), a network-cable pull (state unavailable in HA within the keepalive window, then recovery), and a child-proofing check (random taps, swipes from every edge, long presses, multi-finger gestures: no escape from the kiosk).

---

## 15. Milestones and acceptance criteria

### M0: Skeleton (½ day)
Repo layout, pyproject, config models, logging, fakes, CI, `--simulate` boot.
**Acceptance:**
- `make lint test` passes in CI.
- `pikioskd --check-config config/config.example.yaml` exits 0. A broken config exits 2 with a readable message.

### M1: MVP: HA plays videos (core value)
mpv unit plus IPC client, MQTT connect with LWT, discovery for: play, pause, stop buttons, the text (play URL/ID), the volume number, the mute switch, and the state and title sensors. The video select (catalog). systemd units, auto-login, and boot to a black idle screen. Basic install.sh.
**Acceptance:**
1. On a fresh Pi OS Bookworm 64-bit, `sudo ./install.sh` followed by editing the config and rebooting → the device "Kids Kiosk" appears in HA with the M1 entities, with no HA YAML.
2. Setting the text entity to a YouTube URL **or** an 11-char ID plays it full-screen within 10 s on a 50 Mbit link, with **no** YouTube UI, recommendations, ads, comments, cursor or OSD visible at any time, including at the end of the video.
3. Play, pause, stop, volume and mute act within 1 s, and their state reflects in HA within 2 s.
4. Choosing a catalog item in the select plays it. Unknown names are rejected and logged.
5. `systemctl --user kill pikiosk-mpv` → mpv restarts within 5 s and HA commands work again without restarting `pikioskd`.
6. Unplug Ethernet for 2 min → HA shows unavailable (LWT). Replug → available within 60 s with the correct state.
7. Reboot → the kiosk returns to idle (black screen) by itself, with volume and mute restored.
8. Tests for the M1 modules exist and pass.

### M2: Full HA device, screen control and HA YAML
All remaining entities from 7.5: next and previous, the screen light, sleep timeout and enable, after-video, the Touch UI switch (state only until M5), position, duration, thumbnail, CPU temperature, uptime and IP. Backlight discovery and udev, the idle sleep timer, wake rules, persistence, the HA package YAML and dashboard card.
**Acceptance:**
1. All 22 entities in 7.5 exist with the given default entity ids, units, device classes and categories (verified against golden payloads).
2. Works on **both** Touch Display 2 and the original 7" display: light on/off and brightness 1–255 visibly change the panel. `max_brightness` is auto-detected. min_brightness keeps the panel visibly on.
3. Sleep: with sleep enabled at 1 min and nothing playing, the screen turns off after 60 s (±2 s). A touch wakes it and that touch doesn't start a video. Playback never sleeps the screen.
4. After-video `loop`, `next`, `stop` and `blank` behave as in 6.3 for a single video and a playlist.
5. `ha/packages/pikiosk.yaml` loads in HA without errors (checked with *Developer tools → YAML → Check configuration*). The universal media_player shows title, artwork, position and duration, and play, pause, volume, mute, source select, next and previous all work from the HA media card. The 3 example automations work (tested by triggering manually).
6. Every setting (volume, mute, brightness, screen state, sleep settings, after-video, UI lock, selection) survives a reboot and a PoE power-pull.
7. Position updates at most every 5 s while playing, and not at all when paused or idle.

### M3: Reliability hardening
The watchdog (sd_notify), mpv hang detection, the UI heartbeat restart, the yt-dlp update timer with smoke test, rollback and auto-heal, error classification, the coalesced outgoing queue, the queued play command while mpv is down, the journald cap, the optional hardware watchdog, and maintenance buttons (reload config, update yt-dlp, restart).
**Acceptance:**
1. `kill -STOP $(pidof pikioskd)` → systemd restarts it within 45 s (watchdog).
2. Simulated mpv hang (`kill -STOP` mpv) → detected and mpv restarted within 30 s. HA briefly shows player entities unavailable, then they recover.
3. `systemctl --user start pikiosk-ytdlp-update` upgrades yt-dlp, and a forced failing smoke test rolls back. The version sensor updates.
4. Three injected extractor failures trigger one auto-update, rate-limited to once per hour.
5. 72-hour soak test (videos looping, MQTT broker restarted every 6 h, network cut twice): no memory growth above 50 MB RSS for `pikioskd`, no manual intervention, and logs under the journald cap.
6. Config hot reload adds or removes catalog items, and the select options update in HA without a restart.

### M4: Offline mode
CacheMgr, sync window, LRU eviction, prefer-offline playback, `only_offline`, the cache sensor, the sync button, and locally generated thumbnails.
**Acceptance:**
1. With `offline.enabled`, all catalog videos (and playlist items up to `max_items`) download within the window. `cache/index.json` is correct.
2. With the WAN blocked at the router, every cached approved video plays from HA and the UI, and thumbnails still show.
3. Eviction keeps the cache within `max_size_gb` and never deletes current catalog items unless space is insufficient (warning logged).
4. Streaming playback isn't disturbed by a running sync (the sync pauses during streaming).

### M5: Touch UI and parent lock
aiohttp server, grid UI, WebSocket, Chromium kiosk unit, evdev monitor with grab, the touch policy during playback, the locked screen, the optional PIN unlock, and the cursor and keyboard lockdown.
**Acceptance:**
1. After boot the grid of approved thumbnails fills the screen (correct orientation on both displays). Tapping a thumbnail plays it full-screen. At the end, the configured after-video behaviour happens and the grid comes back.
2. Child-proofing: 5 minutes of random taps, swipes from all edges, long presses, pinches and multi-finger gestures on the grid and during playback produce **no** way out (no desktop, panel, keyboard, browser UI, context menu, zoom or text selection), and nothing except approved videos plays.
3. Turning the HA Touch UI switch OFF shows the locked screen within 1 s and ignores all touches (except wake and idle reset). ON restores the grid. The state persists across reboot.
4. The tap debounce works: 10 rapid taps start exactly one video.
5. If PIN unlock is enabled: the corner hold plus the correct PIN unlocks for 15 minutes, and wrong PINs trigger the cooldown.
6. RAM headroom: with Chromium plus mpv playing 720p, available memory stays above 500 MB on a 4 GB Pi 5, and CPU temperature stays under 75 °C with the PoE HAT fan at default curve (record the numbers).

### M6+: Extras (backlog; each item is its own small milestone)
See section 16.

---

## 16. Extensibility and future features
The architecture should make these simple to add (each as a new module, plus commands in the controller registry, plus discovery entries):
- **Command registry:** commands are registered with a decorator (`@command("set_volume", schema=VolumeArgs)`), and the MQTT bridge, the `cmd/json` topic, the web API and the `pikiosk-ctl` CLI all dispatch through it. New entities are declared in one `ENTITIES` table (component, object, discovery fields, state getter, command name), and the discovery, golden tests and README entity table are generated from it.
- **Source plugins:** a `MediaSource` protocol (`resolve(ref) → PlayableItem`), with implementations for YouTube (yt-dlp), local files, and later Jellyfin, Plex, Nextcloud or DLNA.
- Screen-time limits: daily watch budget, with a "time's up" calm screen and HA sensors for minutes watched today.
- Schedules on the device (fallback if HA is down): quiet hours where only certain items are allowed, or the screen stays off.
- Favourites ordering by play count. A "now and next" visual timeline for routine support.
- Volume ramp-down at bedtime. A night-light mode (screen dim warm image).
- A SponsorBlock-style intro and outro skip for cached content (`--sponsorblock-remove` at download time).
- An HA `image` entity for the thumbnail. HA `update` entity for pi-kiosk and yt-dlp versions (MQTT `update` component).
- Multi-kiosk: several devices share one config repo, with ids that differ by `device.id`.
- An optional switch-access or big-button input (USB/Bluetooth single switch, common for Angelman AAC setups) mapped to commands via evdev.
- OTA updates of pi-kiosk itself via git tag plus an HA `update` entity.

---

## 17. Open questions (confirm before or during M1; defaults in **bold** are used if unanswered)
1. **HA version and MQTT broker:** which HA version and broker (Mosquitto add-on?), and is TLS needed? → **Assume HA ≥ 2025.10, Mosquitto add-on, port 1883, no TLS on LAN.**
2. **Which display(s) exactly:** Touch Display 2 (7" or 5"?) or the original 7"? And orientation: landscape (rotate TD2 90°) or portrait? → **Support both; default landscape.**
3. **Which PoE HAT model** (official PoE+ HAT variant vs third-party), and does the switch provide 802.3at (PoE+)? Fan noise matters in a bedroom: should the fan curve be quietened? → **Assume 802.3at; document power budget; no fan changes.**
4. **Audio output:** USB speaker, I2S DAC/amp HAT (does it physically fit with the PoE HAT?), or Bluetooth? → **USB speaker via PipeWire default sink.**
5. **Approved content source:** maintained only in YAML, or also from an HA-managed list (for example an HA `input_select`, a YouTube playlist the parent curates, or a todo list)? → **YAML catalog plus support for curated YouTube playlists; HA-managed list is a post-v1 option.**
6. **Ad-hoc URLs from HA:** allowed (`allow_unlisted: true`), or catalog only for maximum safety? → **Allowed from HA only, never from the touch UI.**
7. **Touch behaviour during playback** for your daughter: ignore taps (default), tap to pause, or tap to return to the grid? Is a long press useful, or will it trigger by accident? → **Ignore; long press disabled.**
8. **Should a paused video count as idle** for the sleep timer? → **Yes.**
9. **On-device parent unlock (PIN)** wanted, or HA-only lock? → **HA only.**
10. **Offline mode:** wanted from day one? Storage (SD vs NVMe) and size budget? → **Off by default; 20 GB cap when enabled.**
11. **Resume:** resume the last video after a reboot or power cut, or always start idle? Resume each video where it left off? → **Start idle; per-video resume off for short kids' videos (configurable).**
12. **Maximum volume cap:** what level is safe for the room or speaker? → **80 %.**
13. **Locked screen look:** black, a calm logo or photo, or a visual clock or routine image? → **Black.**
14. **Hostname, `device.id` and name:** → **`kiosk` / `kids_kiosk` / "Kids Kiosk".**
15. **YouTube Premium / cookies:** is a Premium account available to cut bot-check friction? (No ads appear either way, because yt-dlp fetches the raw streams.) → **No cookies.**
16. **Raspberry Pi OS version:** stay on Bookworm as agreed, or target Trixie (the newer Debian 13-based release) for new installs? → **Bookworm; keep the installer tolerant of Trixie (detect, warn, continue with `--force`).**
17. **Remote access for maintenance** (SSH keys only? Tailscale?) → **SSH with keys; password login disabled by installer flag `--harden-ssh`.**

---

## 18. Definition of done (whole project)
- Every milestone M1–M5 meets its acceptance criteria on real hardware with both displays (or with the display the user has, plus a documented test of the other via config).
- README covers: hardware assembly notes, OS imaging (Raspberry Pi Imager settings: user, SSH, hostname), install, config reference (generated from the pydantic models), HA setup (MQTT integration, package YAML, dashboard), touch UI and lock, offline mode, troubleshooting (yt-dlp errors, no backlight found, black screen, touch rotated, audio device selection, PoE power warnings from `get_throttled`), and the update procedure.
- `make lint test` is green. Golden discovery snapshots are committed. There's a 72-hour soak report in `docs/`.
- No secrets in the repo. The installer never overwrites user config. Uninstall works.
