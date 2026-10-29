from __future__ import annotations

import json
from pathlib import Path

from pikiosk.mqtt.discovery import DiscoveryContext, build_component_payloads, build_device_payload
from pikiosk.mqtt.topics import Topics

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "discovery"


def test_play_button_matches_spec(example_config) -> None:
    payloads = build_component_payloads(example_config, DiscoveryContext())
    topics = Topics(example_config)
    play = payloads[topics.discovery_topic("button", "play")]
    assert play["name"] == "Play"
    assert play["unique_id"] == "pikiosk_kids_kiosk_play"
    assert play["default_entity_id"] == "button.kids_kiosk_play"
    assert play["command_topic"] == "pikiosk/kids_kiosk/cmd/play"
    assert play["payload_press"] == "PRESS"
    assert play["icon"] == "mdi:play"
    assert play["availability_mode"] == "all"
    assert len(play["availability"]) == 2


def test_select_options(example_config) -> None:
    payloads = build_component_payloads(example_config, DiscoveryContext())
    video = payloads[Topics(example_config).discovery_topic("select", "video")]
    assert video["options"] == [
        "Baby Shark",
        "Sesame Street Songs",
        "Bath song",
        "Calm lights (local)",
    ]


def test_volume_max(example_config) -> None:
    volume = build_component_payloads(example_config)[
        Topics(example_config).discovery_topic("number", "volume")
    ]
    assert volume["max"] == 80


def test_legacy_object_id(example_config) -> None:
    config = example_config.model_copy(
        update={"mqtt": example_config.mqtt.model_copy(update={"legacy_object_id": True})}
    )
    play = build_component_payloads(config)[Topics(config).discovery_topic("button", "play")]
    assert play["object_id"] == "kids_kiosk_play"
    assert "default_entity_id" not in play


def test_device_mode_cmps(example_config) -> None:
    payload = build_device_payload(example_config)
    assert "cmps" in payload
    assert payload["cmps"]["play"]["p"] == "button"


def test_golden_snapshots(example_config) -> None:
    # Pin sw_version so bumping __version__ does not churn every golden file.
    payloads = build_component_payloads(example_config, DiscoveryContext(sw_version="1.0.0"))
    assert GOLDEN.is_dir()
    for topic, payload in payloads.items():
        object_id = topic.rstrip("/").split("/")[-2]
        file = GOLDEN / f"{object_id}.json"
        assert file.exists(), topic
        assert json.loads(file.read_text(encoding="utf-8")) == payload
