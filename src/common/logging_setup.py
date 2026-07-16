"""Structured logging for the pipeline: human lines to stdout + JSON-lines file.

``configure_logging`` is idempotent and wires two handlers on the root logger:

* stdout with a compact human-readable format (CI-visible), and
* an optional JSON-lines file (one JSON object per record) under ``output/``.

Structured fields (``stage``, ``event``, ``outcome``, ``duration_s`` and any
extras) are attached to a :class:`logging.LogRecord` via ``extra=`` and rendered
by both formatters. Use :func:`log_event` for structured records.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

__all__ = ["configure_logging", "get_logger", "log_event"]

_STRUCTURED_KEYS = ("stage", "event", "outcome", "duration_s")

# LogRecord attributes that are always present; everything else the caller put
# on the record via extra= is treated as a structured "extra" field for JSON.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}

_configured = False


class _HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        parts = []
        for key in _STRUCTURED_KEYS:
            val = getattr(record, key, None)
            if val is not None and val != "":
                if key == "duration_s":
                    parts.append(f"dur={float(val):.3f}s")
                else:
                    parts.append(f"{key}={val}")
        prefix = ("  " + " ".join(parts)) if parts else ""
        return f"{base}{prefix}"


class _JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _STRUCTURED_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        # Any additional structured extras.
        for key, val in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                try:
                    json.dumps(val)
                    payload[key] = val
                except (TypeError, ValueError):
                    payload[key] = repr(val)
        return json.dumps(payload, default=str)


def configure_logging(*, level: int = logging.INFO, json_path: Path | None = None) -> None:
    """Configure root logging once. Safe to call repeatedly (idempotent)."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(
        _HumanFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    )
    root.addHandler(console)

    if json_path is not None:
        json_path = Path(json_path)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(json_path, mode="w", encoding="utf-8")
        file_handler.setFormatter(_JsonLinesFormatter())
        root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger, stage, event, outcome="ok", duration_s=None, **fields) -> None:
    """Emit a structured log record.

    Level is WARNING/ERROR when ``outcome`` signals trouble, else INFO. Extra
    ``fields`` are attached to the record and rendered in both formatters.
    """
    msg = fields.pop("msg", None)
    extra = {"stage": stage, "event": event, "outcome": outcome}
    if duration_s is not None:
        extra["duration_s"] = duration_s
    # Rename any field that would collide with a reserved LogRecord attribute
    # (e.g. "name", "module", "args") — otherwise logging raises KeyError.
    for key, val in fields.items():
        safe_key = f"x_{key}" if key in _RESERVED else key
        extra[safe_key] = val
    if outcome in ("failed", "error"):
        level = logging.ERROR
    elif outcome in ("degraded", "skipped", "partial"):
        level = logging.WARNING
    else:
        level = logging.INFO
    logger.log(level, msg or f"{stage}:{event}", extra=extra)
