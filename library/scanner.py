"""Filesystem walker, tag reader and SQLite indexer.

Design notes:

* **Polling, not inotify.** The brief calls for it and it's the right call: the
  music root is often a USB stick or a network mount, and inotify silently does
  nothing on several of those. An 8-second poll is imperceptible for "tracks
  appear after a download" and behaves identically everywhere.

* **Stat-first.** A rescan stats every file and only opens the ones whose mtime
  or size moved. Re-tagging 500 unchanged files on every tick would peg the Pi's
  CPU; stat-ing them costs almost nothing.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from engine import config
from engine.logging_setup import get_logger

from . import covers, db

log = get_logger("scanner")

AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".flac", ".opus", ".ogg", ".oga",
              ".wav", ".aac", ".aiff", ".aif", ".wma"}

# Tag keys per container, in priority order.
_TAG_MAP: Dict[str, Tuple[str, ...]] = {
    "title":        ("TIT2", "\xa9nam", "title"),
    "artist":       ("TPE1", "\xa9ART", "artist"),
    "album":        ("TALB", "\xa9alb", "album"),
    "album_artist": ("TPE2", "aART", "albumartist", "album_artist"),
    "year":         ("TDRC", "TYER", "\xa9day", "date", "year", "originaldate"),
    "track":        ("TRCK", "trkn", "tracknumber", "track"),
}


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _first_tag(tags: Any, keys: Tuple[str, ...]) -> Optional[str]:
    if tags is None:
        return None
    for key in keys:
        try:
            if key not in tags:
                continue
            value = tags[key]
        except (TypeError, KeyError):
            continue

        if isinstance(value, list) and value:
            value = value[0]
        if value is None:
            continue

        # MP4 'trkn' is [(number, total)]
        if isinstance(value, tuple):
            value = value[0]

        text = str(value).strip()
        if text:
            return text
    return None


def _parse_track_number(raw: Optional[str]) -> Optional[int]:
    """Handles '3', '3/12', '03' and the odd '  3 '."""
    if not raw:
        return None
    head = str(raw).split("/")[0].strip()
    digits = "".join(c for c in head if c.isdigit())
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def _parse_year(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = str(raw).strip()
    for i in range(len(text) - 3):
        chunk = text[i:i + 4]
        if chunk.isdigit() and 1900 <= int(chunk) <= 2999:
            return chunk
    return None


def read_metadata(path: Path) -> Optional[Dict[str, Any]]:
    """Open a file once and pull out everything we index, art included."""
    try:
        import mutagen

        audio = mutagen.File(str(path))
    except Exception as exc:
        log.debug("Unreadable audio file %s: %s", path, exc)
        return None

    if audio is None:
        return None

    tags = getattr(audio, "tags", None)
    info = getattr(audio, "info", None)

    title = _first_tag(tags, _TAG_MAP["title"]) or path.stem
    cover_bytes = covers.extract_cover_bytes(audio)

    return {
        "title": title,
        "artist": _first_tag(tags, _TAG_MAP["artist"]),
        "album": _first_tag(tags, _TAG_MAP["album"]),
        "album_artist": _first_tag(tags, _TAG_MAP["album_artist"]),
        "year": _parse_year(_first_tag(tags, _TAG_MAP["year"])),
        "track_number": _parse_track_number(_first_tag(tags, _TAG_MAP["track"])),
        "duration_s": int(getattr(info, "length", 0) or 0),
        "cover_hash": covers.cache_cover(cover_bytes),
    }


def _dominant_artist(entries: List[Dict[str, Any]]) -> Optional[str]:
    """Name a folder's artist by majority, not unanimity.

    A single-artist album routinely carries a couple of odd tags — a featured
    credit written as "Drake/JAY-Z", or one untagged file. Requiring every
    track to agree labelled those albums "Various Artists", which is wrong and
    looks broken next to the cover. Majority gets it right and still reports
    genuine compilations honestly.
    """
    names = [e["album_artist"].strip() for e in entries
             if e.get("album_artist") and str(e["album_artist"]).strip()]
    if not names:
        return None
    top, count = Counter(names).most_common(1)[0]
    return top if count * 2 > len(entries) else "Various Artists"


def _folder_display_name(relative_dir: str) -> str:
    """Bottom-most directory name, or a bucket for loose files at the root."""
    if not relative_dir or relative_dir == ".":
        return "Singles"
    return Path(relative_dir).name


def scan(force: bool = False) -> Dict[str, int]:
    """Walk the music root and reconcile the index with what's on disk."""
    root = config.music_dir()
    started = time.perf_counter()
    db.init()

    known = {} if force else db.known_track_stamps()
    seen_paths: set[str] = set()
    ensured_dirs: set[str] = set()
    by_folder: Dict[str, List[Dict[str, Any]]] = {}
    added = updated = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Never descend into our own cache, or hidden/system dirs.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            if filename.startswith("."):
                continue
            full = Path(dirpath) / filename
            if full.suffix.lower() not in AUDIO_EXTS:
                continue

            try:
                stat = full.stat()
            except OSError:
                continue

            relative = str(full.relative_to(root)).replace("\\", "/")
            seen_paths.add(relative)

            relative_dir = str(Path(relative).parent).replace("\\", "/")
            if relative_dir == ".":
                relative_dir = ""

            previous = known.get(relative)
            unchanged = (
                previous is not None
                and previous[0] is not None
                and abs((previous[0] or 0) - stat.st_mtime) < 1.0
                and previous[1] == stat.st_size
            )

            if unchanged:
                # Still needs to count toward its folder's aggregates.
                row = db.track_by_path(relative)
                if row is not None:
                    by_folder.setdefault(relative_dir, []).append({
                        "duration_s": row["duration_s"] or 0,
                        "cover_hash": row["cover_hash"],
                        "album_artist": row["artist"],
                        "album": row["album"],
                    })
                    continue

            meta = read_metadata(full)
            if meta is None:
                continue

            # Folder row must exist before its tracks can reference it.
            if relative_dir not in ensured_dirs:
                db.ensure_folder(_stable_id(relative_dir), relative_dir,
                                 _folder_display_name(relative_dir))
                ensured_dirs.add(relative_dir)

            track_row = {
                "id": _stable_id(relative),
                "folder_id": _stable_id(relative_dir),
                "path": relative,
                "title": meta["title"],
                "artist": meta["artist"] or meta["album_artist"],
                "album": meta["album"] or _folder_display_name(relative_dir),
                "year": meta["year"],
                "track_number": meta["track_number"],
                "duration_s": meta["duration_s"],
                "cover_hash": meta["cover_hash"],
                "file_size": stat.st_size,
                "mtime": stat.st_mtime,
                "updated_at": time.time(),
            }
            db.upsert_track(track_row)
            if previous is None:
                added += 1
            else:
                updated += 1

            by_folder.setdefault(relative_dir, []).append({
                "duration_s": meta["duration_s"],
                "cover_hash": meta["cover_hash"],
                "album_artist": meta["album_artist"] or meta["artist"],
                "album": meta["album"],
            })

    # Aggregate folders from whatever their tracks actually say.
    for relative_dir, entries in by_folder.items():
        cover_hash = next((e["cover_hash"] for e in entries if e.get("cover_hash")), None)
        db.upsert_folder({
            "id": _stable_id(relative_dir),
            "path": relative_dir,
            "name": _folder_display_name(relative_dir),
            "artist": _dominant_artist(entries),
            "track_count": len(entries),
            "duration_s": sum(int(e["duration_s"] or 0) for e in entries),
            "cover_hash": cover_hash,
            "updated_at": time.time(),
        })

    removed = db.delete_tracks(set(known) - seen_paths) if not force else 0
    db.prune_empty_folders()
    db.conn().commit()

    elapsed = time.perf_counter() - started
    summary = {"added": added, "updated": updated, "removed": removed,
               "total": len(seen_paths), "elapsed_ms": int(elapsed * 1000)}

    if added or updated or removed or force:
        log.info("Scan: %d new, %d changed, %d gone, %d total (%dms)",
                 added, updated, removed, len(seen_paths), summary["elapsed_ms"])
    return summary


class Scanner(threading.Thread):
    """Background poller. `on_change` fires only when something actually moved."""

    def __init__(self, on_change: Optional[Callable[[Dict[str, int]], None]] = None) -> None:
        super().__init__(name="library-scanner", daemon=True)
        self._on_change = on_change or (lambda _summary: None)
        self._stop = threading.Event()
        self._wake = threading.Event()

    def run(self) -> None:
        try:
            summary = scan(force=True)
            log.info("Initial scan indexed %d tracks in %dms",
                     summary["total"], summary["elapsed_ms"])
            self._on_change(summary)
        except Exception:
            log.exception("Initial library scan failed")

        while not self._stop.is_set():
            interval = max(2, int(config.get("scan_interval_s")))
            # Either the interval elapses or someone nudges us after a download.
            self._wake.wait(timeout=interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                summary = scan()
                if summary["added"] or summary["updated"] or summary["removed"]:
                    self._on_change(summary)
            except Exception:
                log.exception("Library rescan failed")

    def nudge(self) -> None:
        """Ask for a rescan now — called when a download reports a landed file."""
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
