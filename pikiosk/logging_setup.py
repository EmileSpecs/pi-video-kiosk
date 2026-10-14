"""Journald-friendly logging. Secrets are never written."""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
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


DEFAULT_LOG_FILE = "~/.cache/pi-kiosk/pikioskd.log"


def setup_logging(config: LoggingConfig, *, default_file: str | None = None) -> None:
    """Log to stdout (journald) and, when a path is known, to a rotating file.

    The file is the fallback for devices where the user journal is empty, so
    `logging.file: null` still writes to `default_file` when one is given.
    """
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(config.level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if config.as_json else KeyValueFormatter())
    root.addHandler(handler)
    target = config.file or default_file
    if target:
        path = Path(target).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        except OSError as exc:
            root.warning("log file unavailable path=%s error=%s", path, exc)
            return
        file_handler.setFormatter(JsonFormatter() if config.as_json else _TimestampedKeyValue())
        root.addHandler(file_handler)


class _TimestampedKeyValue(KeyValueFormatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        return f"{stamp} {super().format(record)}"
