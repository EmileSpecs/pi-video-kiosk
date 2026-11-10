from __future__ import annotations

from pikiosk.audio import Sink, ensure_route, pick_sink


def test_pick_sink_prefers_usb() -> None:
    sinks = [
        Sink("auto_null", "IDLE", "Dummy Output"),
        Sink("alsa_output.usb-Example-00.analog-stereo", "SUSPENDED", "USB Audio Device"),
        Sink("alsa_output.platform-hdmi.stereo", "IDLE", "Built-in Audio"),
    ]
    assert pick_sink(sinks).name.startswith("alsa_output.usb")


def test_pick_sink_explicit_name() -> None:
    sinks = [
        Sink("a", description="USB"),
        Sink("b", description="Other"),
    ]
    assert pick_sink(sinks, preferred="b").name == "b"


def test_pick_sink_skips_null_only() -> None:
    assert pick_sink([Sink("auto_null", description="Dummy")]) is None


def test_ensure_route_switches_from_null(monkeypatch) -> None:
    sinks = [
        Sink("auto_null", "IDLE", "Dummy Output"),
        Sink("alsa_output.usb-Speaker.analog-stereo", "IDLE", "USB Speaker"),
    ]
    calls: list[str] = []

    monkeypatch.setattr("pikiosk.audio.list_sinks", lambda: sinks)
    monkeypatch.setattr("pikiosk.audio.default_sink", lambda: "auto_null")

    def set_default(name: str) -> bool:
        calls.append(name)
        return True

    monkeypatch.setattr("pikiosk.audio.set_default_sink", set_default)
    monkeypatch.setattr("pikiosk.audio.unmute_sink", lambda name, volume_percent=100: None)
    route = ensure_route("auto")
    assert route.changed is True
    assert route.sink and "usb" in route.sink
    assert calls == [route.sink]


def test_ensure_route_noop_when_already_usb(monkeypatch) -> None:
    sinks = [Sink("alsa_output.usb-Speaker.analog-stereo", "RUNNING", "USB Speaker")]
    monkeypatch.setattr("pikiosk.audio.list_sinks", lambda: sinks)
    monkeypatch.setattr("pikiosk.audio.default_sink", lambda: sinks[0].name)
    monkeypatch.setattr(
        "pikiosk.audio.set_default_sink",
        lambda name: (_ for _ in ()).throw(AssertionError("should not set")),
    )
    monkeypatch.setattr("pikiosk.audio.unmute_sink", lambda name, volume_percent=100: None)
    route = ensure_route("auto")
    assert route.changed is False
    assert route.sink == sinks[0].name
