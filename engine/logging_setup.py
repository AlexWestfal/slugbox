"""Rotating file logging with a session header, plus a last-resort crash hook.

The Pi runs headless, so when something goes wrong the log file is the only
witness. Every process start writes a banner so you can tell runs apart in a
file that's been rotating for months.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from types import TracebackType
from typing import Type

from . import config

_configured = False
_lock = threading.Lock()

_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup(level: str | None = None) -> logging.Logger:
    """Configure root logging once. Safe to call from any module."""
    global _configured
    with _lock:
        root = logging.getLogger()
        if _configured:
            return logging.getLogger(config.APP_NAME)

        config.ensure_dirs()
        level_name = (level or str(config.get("log_level")) or "INFO").upper()
        root.setLevel(getattr(logging, level_name, logging.INFO))

        file_handler = logging.handlers.RotatingFileHandler(
            config.LOG_DIR / f"{config.APP_NAME}.log",
            maxBytes=2_000_000, backupCount=5, encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        root.addHandler(file_handler)

        # systemd captures stdout into the journal; keep it terse there.
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
        root.addHandler(stream)

        # Third-party chatter drowns out our own lines at INFO.
        for noisy in ("urllib3", "spotipy", "yt_dlp", "werkzeug", "asyncio", "charset_normalizer"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        banner = f" {config.APP_NAME} {config.VERSION} — session start {time.strftime(_DATEFMT)} "
        file_handler.stream.write("\n" + banner.center(78, "=") + "\n")
        file_handler.flush()

        _install_crash_handlers()
        _configured = True
        return logging.getLogger(config.APP_NAME)


def _install_crash_handlers() -> None:
    log = logging.getLogger(config.APP_NAME)

    def _excepthook(exc_type: Type[BaseException], exc: BaseException,
                    tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args) -> None:
        if issubclass(args.exc_type, KeyboardInterrupt):
            return
        log.critical("Unhandled exception in thread %s", args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(f"{config.APP_NAME}.{name}")
