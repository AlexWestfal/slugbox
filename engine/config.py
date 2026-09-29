"""Settings registry, persistence and runtime paths.

One source of truth for every tunable. `config.json` lives next to the project
and is read/written by both the web server and the download worker.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, NamedTuple, Tuple

APP_NAME = "slugbox"
VERSION = "1.0.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "library.db"
CACHE_DIR = DATA_DIR / ".cache"
COVER_DIR = CACHE_DIR / "covers"
LOG_DIR = DATA_DIR / "logs"


class Setting(NamedTuple):
    key: str
    default: Any
    kind: str          # str | int | bool | choice
    choices: Tuple[str, ...] = ()
    help: str = ""


# Audio formats spotDL can produce. opus/flac are lossless-ish and bigger; on a
# Pi with an SD card, mp3/m4a are the sane defaults.
SUPPORTED_FORMATS = ("mp3", "m4a", "flac", "opus", "ogg", "wav")
SUPPORTED_BITRATES = ("auto", "disable", "128k", "192k", "256k", "320k")

SETTINGS: Tuple[Setting, ...] = (
    Setting("music_dir", "~/Music", "str",
            help="Root folder scanned for audio and written to by downloads."),
    Setting("format", "mp3", "choice", SUPPORTED_FORMATS,
            help="Output container/codec."),
    Setting("bitrate", "320k", "choice", SUPPORTED_BITRATES,
            help="Target bitrate; 'auto' lets spotDL match the source."),
    Setting("threads", 2, "int",
            help="Parallel track downloads. 2 on a Pi 4 — 4 saturates the CPU "
                 "and makes the kiosk UI stutter."),
    Setting("output_template", "{list-name}/{track-number}. {title} - {artists}.{output-ext}", "str",
            help="spotDL filename template, relative to music_dir."),
    Setting("playlist_numbering", True, "bool",
            help="Number tracks by playlist position rather than album position."),
    Setting("host", "0.0.0.0", "str", help="Bind address."),
    Setting("port", 5000, "int", help="HTTP port."),
    Setting("scan_interval_s", 8, "int",
            help="Seconds between library rescans. Polling beats inotify here — "
                 "it behaves the same on ext4, exFAT and NFS mounts."),
    Setting("ffmpeg", "ffmpeg", "str",
            help="FFmpeg binary path, or 'ffmpeg' to use PATH."),
    Setting("spotify_client_id", "", "str",
            help="Optional. Blank uses spotDL's built-in public credentials."),
    Setting("spotify_client_secret", "", "str", help="See spotify_client_id."),
    Setting("log_level", "INFO", "choice", ("DEBUG", "INFO", "WARNING", "ERROR")),
)

_DEFAULTS: Dict[str, Any] = {s.key: s.default for s in SETTINGS}
_BY_KEY: Dict[str, Setting] = {s.key: s for s in SETTINGS}

_lock = threading.RLock()
_cache: Dict[str, Any] | None = None


def _coerce(setting: Setting, value: Any) -> Any:
    """Force a value into the setting's declared type, falling back to default."""
    try:
        if setting.kind == "int":
            return int(value)
        if setting.kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if setting.kind == "choice":
            value = str(value)
            return value if value in setting.choices else setting.default
        return str(value)
    except (TypeError, ValueError):
        return setting.default


def load() -> Dict[str, Any]:
    """Read config.json, filling in defaults for anything missing or invalid.

    If config.json does not exist, is empty, or is corrupted, it is automatically
    generated with defaults and persisted to disk so the user never gets stuck.
    """
    global _cache
    with _lock:
        if _cache is not None:
            return dict(_cache)

        raw: Dict[str, Any] = {}
        needs_write = False

        if not CONFIG_PATH.exists() or CONFIG_PATH.stat().st_size == 0:
            needs_write = True
        else:
            try:
                # utf-8-sig, not utf-8: editors on Windows (and PowerShell's
                # Set-Content) happily prepend a BOM, and json.loads chokes on
                # it. Reading the file as utf-8 meant a single invisible byte
                # silently reverted every setting to its default.
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError) as exc:
                import logging
                logging.getLogger(APP_NAME).error(
                    "config.json could not be read (%s) — recreating with "
                    "defaults.", exc)
                raw = {}
                needs_write = True
            if not isinstance(raw, dict):
                raw = {}
                needs_write = True

        merged = dict(_DEFAULTS)
        for key, value in raw.items():
            if key in _BY_KEY:
                merged[key] = _coerce(_BY_KEY[key], value)

        # If a non-root user inherited a /root/Music directory from a previous
        # run as root, safely heal it back to ~/Music
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            if str(merged.get("music_dir", "")).startswith("/root/"):
                merged["music_dir"] = "~/Music"
                needs_write = True

        _cache = merged

        if needs_write:
            try:
                CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
                tmp = CONFIG_PATH.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
                tmp.replace(CONFIG_PATH)
            except OSError:
                pass

        return dict(merged)


def save(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Merge `updates` into the config and persist. Unknown keys are ignored."""
    global _cache
    with _lock:
        current = load()
        for key, value in updates.items():
            if key in _BY_KEY:
                current[key] = _coerce(_BY_KEY[key], value)

        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)   # atomic — never leaves a half-written config

        _cache = current
        return dict(current)


def get(key: str) -> Any:
    return load().get(key, _DEFAULTS.get(key))


def music_dir() -> Path:
    """Absolute, expanded music root. Created if absent."""
    raw = str(get("music_dir") or "~/Music")
    if hasattr(os, "geteuid") and os.geteuid() != 0 and raw.startswith("/root/"):
        raw = "~/Music"

    path = Path(os.path.expanduser(raw)).resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # Fallback to current user's ~/Music if configured path is forbidden
        path = (Path.home() / "Music").resolve()
        path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, COVER_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def ffmpeg_path() -> str | None:
    """Resolve the FFmpeg binary, or None if it genuinely isn't there.

    spotDL refuses to construct a Downloader without FFmpeg, so the server
    checks this up front and reports it through /api/settings rather than
    letting a download job die with a confusing traceback.
    """
    configured = str(get("ffmpeg") or "ffmpeg")
    if configured != "ffmpeg":
        return configured if Path(configured).exists() else None

    found = shutil.which("ffmpeg")
    if found:
        return found
    # spotDL's own bundled download location, if the user ran --download-ffmpeg.
    bundled = Path.home() / ".spotdl" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    return str(bundled) if bundled.exists() else None


def describe() -> list[dict]:
    """Setting metadata for the UI to render a settings form from."""
    return [
        {"key": s.key, "kind": s.kind, "choices": list(s.choices),
         "default": s.default, "help": s.help}
        for s in SETTINGS
    ]
