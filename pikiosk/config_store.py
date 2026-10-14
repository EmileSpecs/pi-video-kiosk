"""Read and write config.yaml plus secrets.yaml without dropping !secret tags."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pikiosk.config import (
    ConfigError,
    KioskConfig,
    SecretRef,
    _Loader,
    atomic_write_text,
    load_config,
)


class _Dumper(yaml.SafeDumper):
    pass


def _represent_secret(dumper: yaml.SafeDumper, data: SecretRef) -> yaml.Node:
    return dumper.represent_scalar("!secret", data.key)


_Dumper.add_representer(SecretRef, _represent_secret)


class ConfigStore:
    def __init__(self, config_path: Path, secrets_path: Path) -> None:
        self.config_path = config_path
        self.secrets_path = secrets_path
        self.config = load_config(config_path, secrets_path=secrets_path)
        self.secrets = self._read_secrets()

    def _read_secrets(self) -> dict[str, Any]:
        if not self.secrets_path.exists():
            return {}
        loaded = yaml.load(self.secrets_path.read_text(encoding="utf-8"), Loader=_Loader)
        return loaded if isinstance(loaded, dict) else {}

    def reload(self) -> KioskConfig:
        self.secrets = self._read_secrets()
        self.config = load_config(self.config_path, secrets_path=self.secrets_path)
        return self.config

    def save(self, config: KioskConfig, secrets: dict[str, Any]) -> None:
        data = config.model_dump(mode="json", by_alias=True)
        data["mqtt"]["password"] = SecretRef("mqtt_password")
        data["ui"]["parent_unlock"]["pin"] = SecretRef("ui_pin")
        data["admin"]["password"] = SecretRef("admin_password")
        text = yaml.dump(data, Dumper=_Dumper, sort_keys=False)
        atomic_write_text(self.config_path, text)
        secret_text = yaml.safe_dump(secrets, sort_keys=False)
        atomic_write_text(self.secrets_path, secret_text)
        try:
            self.config = load_config(self.config_path, secrets_path=self.secrets_path)
        except ConfigError:
            raise
        self.secrets = secrets
