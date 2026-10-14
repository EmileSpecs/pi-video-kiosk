"""Configuration models, YAML loading, and !secret resolution."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pikiosk.catalog import (
    PLAYLIST_ID_RE,
    VIDEO_ID_RE,
    CatalogError,
    parse_ref,
)

DEVICE_ID_RE = re.compile(r"^[a-z0-9_]{3,32}$")
SCHEMA_VERSION = 1


class ConfigError(Exception):
    """A config file is missing or invalid. Process should exit 2."""


class SecretRef:
    def __init__(self, key: str) -> None:
        self.key = key

    def __repr__(self) -> str:
        return f"!secret {self.key}"


class _Loader(yaml.SafeLoader):
    pass


def _secret_constructor(loader: yaml.SafeLoader, node: yaml.Node) -> SecretRef:
    value = loader.construct_scalar(node)  # type: ignore[arg-type]
    return SecretRef(str(value))


_Loader.add_constructor("!secret", _secret_constructor)


class TlsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    ca_file: str | None = None
    insecure: bool = False


class ReconnectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_delay_s: float = 1
    max_delay_s: float = 60


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    manufacturer: str = "DIY"
    model: str = "Raspberry Pi 5 Video Kiosk"
    suggested_area: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if not DEVICE_ID_RE.match(value):
            raise ValueError("device.id must match ^[a-z0-9_]{3,32}$")
        return value


class MqttConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    keepalive: int = 30
    base_topic: str = "pikiosk"
    discovery_prefix: str = "homeassistant"
    tls: TlsConfig = Field(default_factory=TlsConfig)
    reconnect: ReconnectConfig = Field(default_factory=ReconnectConfig)
    position_publish_interval_s: float = 5
    discovery_mode: Literal["component", "device"] = "component"
    legacy_object_id: bool = False
    image_entity: bool = False

    def resolved_client_id(self, device_id: str) -> str:
        return self.client_id or f"pikiosk-{device_id}"

    def connection_key(self, device_id: str) -> tuple[object, ...]:
        tls = self.tls
        return (
            self.host,
            self.port,
            self.username,
            self.password,
            self.client_id,
            self.keepalive,
            self.base_topic,
            self.discovery_prefix,
            tls.enabled,
            tls.ca_file,
            tls.insecure,
            device_id,
        )


class PlaybackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # H.264 up to 720p merged with AAC; format 18 (360p muxed) and then
    # anything as fallbacks. yt-dlp downloads this, mpv plays the file.
    ytdl_format: str = "bv*[height<=?720][vcodec^=avc1]+ba[acodec^=mp4a]/18/b"
    download_timeout_s: int = 300
    default_volume: int = 40
    max_volume: int = 80
    start_muted: bool = False
    after_video: Literal["loop", "next", "stop", "blank"] = "stop"
    resume_on_boot: bool = False
    resume_position: bool = True
    allow_unlisted: bool = True
    allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "music.youtube.com",
        ]
    )
    prefer_offline: bool = True
    audio_device: str = "auto"
    network_timeout_s: int = 30
    after_video_scope: Literal["item", "playlist"] = "playlist"
    file_roots: list[str] = Field(default_factory=lambda: ["/home/kiosk/Videos"])

    @field_validator("default_volume", "max_volume")
    @classmethod
    def _volume(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("volume must be 0-100")
        return value

    @field_validator("download_timeout_s")
    @classmethod
    def _timeout(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("download_timeout_s must be 30-3600")
        return value

    @model_validator(mode="after")
    def _cap(self) -> PlaybackConfig:
        if self.max_volume < self.default_volume:
            raise ValueError("max_volume must be greater than or equal to default_volume")
        return self


class ScreenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backlight_device: str = "auto"
    output_name: str = "auto"
    rotation: Literal[0, 90, 180, 270] = 0
    default_brightness: int = 70
    min_brightness: int = 5
    blank_method: Literal["backlight", "output", "both"] = "backlight"
    sleep_enabled: bool = True
    sleep_timeout_min: int = 10
    wake_on_touch: bool = True
    swallow_wake_touch: bool = True
    dim_before_sleep_s: int = 15

    @field_validator("default_brightness", "min_brightness")
    @classmethod
    def _percent(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("brightness percent must be 0-100")
        return value

    @field_validator("sleep_timeout_min")
    @classmethod
    def _sleep(cls, value: int) -> int:
        if not 1 <= value <= 240:
            raise ValueError("sleep_timeout_min must be 1-240")
        return value


class ParentUnlockConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    hold_corner: Literal["top_right", "top_left", "bottom_right", "bottom_left"] = "top_right"
    hold_s: int = 5
    pin: str = ""


class UiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 8088
    locked: bool = False
    locked_screen: Literal["black", "logo", "clock"] = "black"
    touch_during_playback: Literal["ignore", "pause_toggle", "stop_to_grid"] = "ignore"
    long_press_stop_s: float = 0
    columns: int = 3
    admin_enabled: bool = False
    show_titles: bool = False
    parent_unlock: ParentUnlockConfig = Field(default_factory=ParentUnlockConfig)


class AdminConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    bind: str = "0.0.0.0"
    port: int = 8090
    password: str = ""


class OfflineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    directory: str = "~/.cache/pi-kiosk/videos"
    max_size_gb: float = 20
    sync_interval_h: float = 12
    download_window: str | None = "01:00-06:00"
    rate_limit: str | None = None
    only_offline: bool = False
    delete_after_days: int = 7


class YtdlpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    update_channel: Literal["stable", "nightly"] = "stable"
    extra_args: list[str] = Field(default_factory=list)
    cookies_file: str | None = None
    test_id: str = "XqZsoesa55w"


class InputConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    touch_device: str = "auto"


class TelemetryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_s: int = 60


class LoggingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str | None = None
    as_json: bool = Field(default=False, alias="json")


class UpdateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto: bool = True
    repo: str | None = None
    ref: str = "main"
    interval_min: int = 15
    only_when_idle: bool = True


class CatalogItemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: Literal["video", "playlist", "file"]
    id: str | None = None
    url: str | None = None
    path: str | None = None
    shuffle: bool = False
    max_items: int | None = None
    start: float | None = None
    thumbnail: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not 1 <= len(value) <= 64:
            raise ValueError("item name must be 1-64 characters")
        return value

    @model_validator(mode="after")
    def _one_source(self) -> CatalogItemConfig:
        present = [value is not None for value in (self.id, self.url, self.path)]
        if sum(present) != 1:
            raise ValueError(f"{self.name!r} needs exactly one of id, url or path")
        if self.type == "file" and self.path is None:
            raise ValueError(f"{self.name!r} file items need a path")
        if self.type != "file" and self.path is not None:
            raise ValueError(f"{self.name!r} path is only valid for file items")
        if self.id is not None and self.type == "video" and not VIDEO_ID_RE.match(self.id):
            raise ValueError(f"{self.name!r} video id is invalid")
        if self.id is not None and self.type == "playlist" and not PLAYLIST_ID_RE.match(self.id):
            raise ValueError(f"{self.name!r} playlist id is invalid")
        if self.url is not None:
            try:
                ref = parse_ref(self.url)
            except CatalogError as exc:
                raise ValueError(f"{self.name!r} url: {exc}") from exc
            if self.type == "video" and ref.kind != "video":
                raise ValueError(f"{self.name!r} url is not a video")
            if self.type == "playlist" and ref.kind != "playlist":
                raise ValueError(f"{self.name!r} url is not a playlist")
        return self


class CatalogConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CatalogItemConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> CatalogConfig:
        names = [item.name for item in self.items]
        if len(names) != len(set(names)):
            raise ValueError("catalog item names must be unique")
        return self


class KioskConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device: DeviceConfig
    mqtt: MqttConfig
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)
    screen: ScreenConfig = Field(default_factory=ScreenConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    offline: OfflineConfig = Field(default_factory=OfflineConfig)
    ytdlp: YtdlpConfig = Field(default_factory=YtdlpConfig)
    input: InputConfig = Field(default_factory=InputConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)

    def file_roots(self) -> list[Path]:
        roots = [Path(path) for path in self.playback.file_roots]
        roots.append(Path(self.offline.directory))
        return roots

    def catalog_items(self) -> list[dict[str, object]]:
        return [item.model_dump() for item in self.catalog.items]

    def support_url(self) -> str:
        if self.update.repo:
            cleaned = self.update.repo.removesuffix(".git")
            if cleaned.startswith("git@"):
                cleaned = "https://" + cleaned[4:].replace(":", "/", 1)
            return cleaned
        return "https://github.com/owner/pi-kiosk"


def _resolve_secrets(value: Any, secrets: dict[str, Any]) -> Any:
    if isinstance(value, SecretRef):
        if value.key not in secrets:
            raise ConfigError(f"secret {value.key!r} is not defined in secrets.yaml")
        return secrets[value.key]
    if isinstance(value, dict):
        return {key: _resolve_secrets(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_secrets(item, secrets) for item in value]
    return value


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def apply_env(data: dict[str, Any], environ: dict[str, str] | None = None) -> None:
    source = environ if environ is not None else dict(os.environ)
    for key, raw in source.items():
        if not key.startswith("PIKIOSK__"):
            continue
        parts = [part.lower() for part in key.split("__")[1:] if part]
        if not parts:
            continue
        cursor: dict[str, Any] = data
        for part in parts[:-1]:
            current = cursor.get(part)
            if not isinstance(current, dict):
                current = {}
                cursor[part] = current
            cursor = current
        cursor[parts[-1]] = _coerce(raw)


def secrets_path_for(config_path: Path) -> Path:
    sibling = config_path.parent / "secrets.yaml"
    if sibling.exists():
        return sibling
    example = config_path.parent / "secrets.example.yaml"
    if example.exists():
        return example
    return sibling


def load_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        loaded = yaml.load(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    return loaded or {}


def load_config(
    path: str | Path,
    *,
    secrets_path: str | Path | None = None,
    environ: dict[str, str] | None = None,
) -> KioskConfig:
    config_path = Path(path)
    raw = load_yaml(config_path)
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a mapping")
    secret_file = Path(secrets_path) if secrets_path else secrets_path_for(config_path)
    secrets: dict[str, Any] = {}
    if secret_file.exists():
        loaded = load_yaml(secret_file)
        if not isinstance(loaded, dict):
            raise ConfigError(f"{secret_file} must contain a mapping")
        secrets = loaded
    try:
        resolved = _resolve_secrets(raw, secrets)
    except ConfigError:
        raise
    if not isinstance(resolved, dict):
        raise ConfigError(f"{config_path} must contain a mapping")
    apply_env(resolved, environ)
    try:
        return KioskConfig.model_validate(resolved)
    except ValidationError as exc:
        lines = [f"{config_path}: invalid configuration"]
        for error in exc.errors():
            loc = ".".join(str(part) for part in error["loc"])
            lines.append(f"  {loc}: {error['msg']}")
        raise ConfigError("\n".join(lines)) from exc


def dump_config(config: KioskConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            pass
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.05)
