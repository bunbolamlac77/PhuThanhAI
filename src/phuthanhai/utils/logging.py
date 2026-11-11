"""Structured logging with path redaction.

Privacy rule from the data contract: default logs must not contain full private
paths, prompts, notes or customer names. We therefore log events as one JSON
object per line and redact the user's home directory to ``~`` before emission.
Debug tracebacks go to stderr only, never to the UI or the exported report.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

_LOGGER_NAME = "phuthanhai"
_HOME = str(Path.home())


def redact(value: Any) -> Any:
    """Replace the home directory prefix in strings/containers with ``~``."""

    if isinstance(value, str):
        return value.replace(_HOME, "~")
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure(*, level: int = logging.INFO, log_file: Path | None = None) -> logging.Logger:
    """Configure the package logger once; return it. Safe to call repeatedly."""

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(JsonLineFormatter())
        logger.addHandler(stream)

    if log_file is not None:
        # Rebuild the file handler so repeated create_app() calls (tests, restarts)
        # cannot accumulate handlers pointing at stale directories.
        for existing in [h for h in logger.handlers if isinstance(h, logging.FileHandler)]:
            logger.removeHandler(existing)
            existing.close()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        # 0600: logs stay private to the current user.
        handler = logging.FileHandler(log_file, encoding="utf-8", mode="a")
        handler.setFormatter(JsonLineFormatter())
        try:
            os.chmod(log_file, 0o600)
        except OSError:
            pass
        logger.addHandler(handler)

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    if not name:
        return logging.getLogger(_LOGGER_NAME)
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"fields": fields})
