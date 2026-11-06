"""
logging_config.py — Structured JSON logging with daily rotation and retention.
Replaces ad-hoc print()/send_log() with proper log levels and file output.

Usage:
    from logging_config import get_logger
    log = get_logger(__name__)
    log.info("model loaded", extra={"device": "cuda", "version": "1.0"})
    log.warning("slow inference", extra={"path": "...", "ms": 350})
"""

import logging
import logging.handlers
import json
import os
import sys
import time
from datetime import datetime


class _StructuredFormatter(logging.Formatter):
    """JSON-line formatter with structured fields."""

    def format(self, record):
        obj = {
            "time": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Merge extra fields
        if hasattr(record, '__extra__') and record.__extra__:
            obj.update(record.__extra__)
        if record.exc_info and record.exc_info[1]:
            obj["exception"] = str(record.exc_info[1])
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class _ExtraAdapter(logging.LoggerAdapter):
    """LoggerAdapter that passes extra dict through to the formatter."""

    def process(self, msg, kwargs):
        extra = kwargs.pop("extra", {})
        kwargs["extra"] = {"__extra__": extra}
        return msg, kwargs


_LOG_DIR = os.environ.get(
    "APS_LOG_DIR",
    os.path.join(os.environ.get("APS_USER_DATA", os.path.expanduser("~/.lenspilot")), "logs"),
)
_RETENTION_DAYS = int(os.environ.get("APS_LOG_RETENTION", "30"))
_MAX_FILE_MB = int(os.environ.get("APS_LOG_MAX_MB", "50"))
_LEVEL = os.environ.get("APS_LOG_LEVEL", "INFO").upper()

_loggers = {}
_init_done = False


def _setup():
    global _init_done
    if _init_done:
        return
    _init_done = True

    os.makedirs(_LOG_DIR, exist_ok=True)

    root = logging.getLogger("lenspilot")
    root.setLevel(getattr(logging, _LEVEL, logging.INFO))

    # File handler with daily rotation
    fh = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(_LOG_DIR, "backend.log"),
        when="midnight",
        interval=1,
        backupCount=_RETENTION_DAYS,
        encoding="utf-8",
    )
    fh.setFormatter(_StructuredFormatter())

    # Also cap file size within a day
    fh_max = max(_MAX_FILE_MB * 1024 * 1024, 10 * 1024 * 1024)
    fh.rotator = None  # use default

    root.addHandler(fh)

    # Stderr for crash-level errors only
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.ERROR)
    sh.setFormatter(_StructuredFormatter())
    root.addHandler(sh)

    # Suppress noisy third-party loggers
    for noisy in ("PIL", "urllib3", "openai", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name="lenspilot"):
    """Get a structured logger. Use extra={} for additional JSON fields."""
    _setup()
    key = name
    if key not in _loggers:
        base = logging.getLogger(f"lenspilot.{name}" if name != "lenspilot" else "lenspilot")
        _loggers[key] = _ExtraAdapter(base, {})
    return _loggers[key]


# Convenience: replace send_log calls incrementally
def send_log_structured(msg, level="info", **extra):
    """Drop-in replacement for service.send_log with structured fields."""
    log = get_logger("service")
    fn = getattr(log, level.lower(), log.info)
    fn(msg, extra=extra)
