from __future__ import annotations

from pathlib import Path

from pikiosk.app import App
from pikiosk.config import load_config

ROOT = Path(__file__).resolve().parents[2]


async def test_simulate_boots(tmp_path) -> None:
    loaded = load_config(ROOT / "config" / "config.example.yaml")
    config = loaded.model_copy(
        update={
            "ui": loaded.ui.model_copy(update={"port": 18765}),
            "admin": loaded.admin.model_copy(update={"port": 18766, "enabled": False}),
        }
    )
    app = App(
        config,
        simulate=True,
        config_path=ROOT / "config" / "config.example.yaml",
        secrets_path=ROOT / "config" / "secrets.example.yaml",
        state_path=tmp_path / "state.json",
        backlight_root=tmp_path,
    )
    import asyncio

    task = asyncio.create_task(app.run())
    await app.ready.wait()
    app.stop()
    await task
