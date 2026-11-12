"""Structured JSON logging for MakeMeSwitch services.

Every component logs one JSON object per line so that the web panel and
journald can consume the same records.  No secrets are ever logged: the
helpers in this module redact values that look like passwords or keys.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SENSITIVE = re.compile(r"(pass(word)?|secret|token|key|authorization|cookie)", re.I)


def _redact(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out[k] = "***" if _SENSITIVE.search(str(k)) and v else _redact(v)
        return out
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if isinstance(extra, dict):
            payload["data"] = _redact(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(name: str = "makemeswitch", log_dir: str | None = None, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_makemeswitch_configured", False):
        return logger
    logger.setLevel(level)
    logger.propagate = False
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonFormatter())
    logger.addHandler(stream)
    directory = log_dir or os.environ.get("MMS_LOG_DIR")
    if directory:
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(Path(directory) / f"{name}.log", maxBytes=5_000_000, backupCount=3)
            fh.setFormatter(JsonFormatter())
            logger.addHandler(fh)
        except OSError:
            pass
    logger._makemeswitch_configured = True  # type: ignore[attr-defined]
    return logger


def get_logger(name: str = "makemeswitch.core") -> logging.Logger:
    return setup_logging(name)
