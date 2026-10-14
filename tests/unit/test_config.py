from __future__ import annotations

from pathlib import Path

import pytest

from pikiosk.config import ConfigError, load_config

ROOT = Path(__file__).resolve().parents[2]


def test_example_loads() -> None:
    config = load_config(ROOT / "config" / "config.example.yaml")
    assert config.device.id == "kids_kiosk"
    assert config.mqtt.password == "change-me"
    assert config.admin.password == "change-me"
    assert [item.name for item in config.catalog.items][0] == "Baby Shark"


def test_secret_resolution(tmp_path: Path) -> None:
    (tmp_path / "secrets.yaml").write_text("mqtt_password: hunter2\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "device: {id: abc, name: N}\nmqtt: {host: h, password: !secret mqtt_password}\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path / "config.yaml")
    assert config.mqtt.password == "hunter2"


def test_missing_secret(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "device: {id: abc, name: N}\nmqtt: {host: h, password: !secret missing}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="missing"):
        load_config(tmp_path / "config.yaml")


def test_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.yaml").write_text(
        "device: {id: abc, name: N}\nmqtt: {host: h, port: 1883}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PIKIOSK__MQTT__PORT", "8883")
    monkeypatch.setenv("PIKIOSK__PLAYBACK__MAX_VOLUME", "50")
    monkeypatch.setenv("PIKIOSK__PLAYBACK__DEFAULT_VOLUME", "20")
    config = load_config(tmp_path / "config.yaml")
    assert config.mqtt.port == 8883
    assert config.playback.max_volume == 50


def test_bad_device_id(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        "device: {id: NO, name: N}\nmqtt: {host: h}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="device.id"):
        load_config(tmp_path / "config.yaml")


def test_duplicate_names(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        """
device: {id: abc, name: N}
mqtt: {host: h}
catalog:
  items:
    - {name: Same, type: video, id: AAAAAAAAAAA}
    - {name: Same, type: video, id: BBBBBBBBBBB}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unique"):
        load_config(tmp_path / "config.yaml")


def test_two_sources(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        """
device: {id: abc, name: N}
mqtt: {host: h}
catalog:
  items:
    - {name: Both, type: video, id: AAAAAAAAAAA, url: "https://youtu.be/AAAAAAAAAAA"}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="exactly one"):
        load_config(tmp_path / "config.yaml")


def test_volume_cap(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(
        """
device: {id: abc, name: N}
mqtt: {host: h}
playback: {default_volume: 90, max_volume: 10}
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="max_volume"):
        load_config(tmp_path / "config.yaml")
