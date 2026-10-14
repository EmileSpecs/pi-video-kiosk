"""Screen orientation: the transform follows the panel's native shape."""

from __future__ import annotations

from pathlib import Path

from pikiosk.screen import orient
from pikiosk.screen.output import OutputInfo, RecordingOutput, parse_outputs, pick_output, wayland_env


def test_transform_matrix() -> None:
    # Touch Display 2: natively portrait.
    assert orient.transform_for("landscape", native_portrait=True) == "90"
    assert orient.transform_for("landscape", native_portrait=True, flip=True) == "270"
    assert orient.transform_for("portrait", native_portrait=True) == "normal"
    assert orient.transform_for("portrait", native_portrait=True, flip=True) == "180"
    # Original 7" display: natively landscape.
    assert orient.transform_for("landscape", native_portrait=False) == "normal"
    assert orient.transform_for("portrait", native_portrait=False) == "90"
    assert orient.transform_for("portrait", native_portrait=False, flip=True) == "270"
    # A hand-set transform wins; the old default of 0 means automatic.
    assert orient.transform_for("landscape", native_portrait=True, rotation=180) == "180"
    assert orient.transform_for("landscape", native_portrait=True, rotation=0) == "90"


def test_parse_outputs_both_json_shapes() -> None:
    modern = [
        {
            "name": "DSI-1",
            "transform": "normal",
            "modes": [{"width": 720, "height": 1280, "current": True}, {"width": 640, "height": 480}],
        },
        {"name": "HDMI-A-1", "modes": [{"width": 1920, "height": 1080}]},
    ]
    outputs = parse_outputs(modern)
    assert outputs[0] == OutputInfo("DSI-1", 720, 1280, "normal")
    assert outputs[0].native_portrait is True
    assert outputs[1].native_portrait is False
    assert pick_output(outputs).name == "DSI-1"
    assert pick_output(outputs, "HDMI-A-1").name == "HDMI-A-1"
    assert pick_output(outputs, "DSI-9") is None
    legacy = parse_outputs({"DSI-1": {}, "HDMI-A-1": {}})
    assert [o.name for o in legacy] == ["DSI-1", "HDMI-A-1"]
    assert parse_outputs("garbage") == []


def test_wayland_env_finds_the_socket(tmp_path: Path) -> None:
    (tmp_path / "wayland-0").write_bytes(b"")
    (tmp_path / "wayland-0.lock").write_bytes(b"")
    env = wayland_env({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert wayland_env({"WAYLAND_DISPLAY": "wayland-1"})["WAYLAND_DISPLAY"] == "wayland-1"
    assert "WAYLAND_DISPLAY" not in wayland_env({"XDG_RUNTIME_DIR": str(tmp_path / "missing")})


async def test_apply_turns_td2_to_landscape_by_default(example_config) -> None:
    output = RecordingOutput("DSI-1", width=720, height=1280)
    name, transform = await orient.apply(example_config, output)
    assert (name, transform) == ("DSI-1", "90")
    assert output.calls == [("DSI-1", True, "90")]

    screen = example_config.screen.model_copy(update={"orientation": "portrait"})
    portrait = example_config.model_copy(update={"screen": screen})
    output = RecordingOutput("DSI-1", width=720, height=1280)
    assert await orient.apply(portrait, output) == ("DSI-1", "normal")
    assert output.calls == []  # already as the panel reports it

    legacy = RecordingOutput("DSI-1", width=800, height=480)
    assert await orient.apply(example_config, legacy) == ("DSI-1", "normal")

    nothing = RecordingOutput(None)
    assert await orient.apply(example_config, nothing) == (None, "normal")


async def test_screen_service_remembers_transform_for_output_blanking(tmp_path: Path, example_config, clock) -> None:
    from pikiosk.screen.backlight import Backlight
    from pikiosk.screen.screen_service import ScreenService

    config = example_config.model_copy(
        update={"screen": example_config.screen.model_copy(update={"blank_method": "output", "output_name": "auto"})}
    )
    output = RecordingOutput("DSI-1", width=720, height=1280)
    screen = ScreenService(config, Backlight(None), output, clock)
    await screen.apply_orientation()
    assert screen.transform == "90" and screen.output_name == "DSI-1"
    await screen.turn_off()
    await screen.turn_on()
    assert output.calls[-2:] == [("DSI-1", False, None), ("DSI-1", True, "90")]
