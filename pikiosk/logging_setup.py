"""Journald-friendly logging. Secrets are never written."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from pikiosk.config import LoggingConfig

REDACT_KEYS = frozenset({"password", "pin", "mqtt_password", "ui_pin", "admin_password", "cookie"})


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = redact(record.getMessage())
        extra = getattr(record, "fields", None)
        suffix = ""
        if isinstance(extra, dict) and extra:
            suffix = " " + " ".join(f"{key}={redact(str(value))}" for key, value in extra.items())
        return f"{record.levelname} {record.name} {message}{suffix}"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload["fields"] = {key: redact(str(value)) for key, value in extra.items()}
        return json.dumps(payload, separators=(",", ":"))


def redact(text: str) -> str:
    lowered = text.lower()
    for key in REDACT_KEYS:
        if key in lowered:
            return "[redacted]"
    return text


def setup_logging(config: LoggingConfig) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(config.level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if config.as_json else KeyValueFormatter())
    root.addHandler(handler)
    if config.file:
        file_handler = logging.FileHandler(config.file)
        file_handler.setFormatter(handler.formatter)
        root.addHandler(file_handler)
