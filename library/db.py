"""SQLite storage: library index plus the download job queue.

WAL mode matters here. Three things touch this database concurrently — the
Flask request threads (reads), the library scanner (writes), and the download
worker (writes) — and in the default rollback journal a single writer blocks
every reader, which shows up as the kiosk UI stalling mid-scan.

Connections are thread-local: SQLite connection objects are not safe to share
across threads, and a pool would be overkill for three consumers.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from engine import config
from engine.logging_setup import get_logger

log = get_logger("db")

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    id          TEXT PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    artist      TEXT,
    track_count INTEGER DEFAULT 0,
    duration_s  INTEGER DEFAULT 0,
    cover_hash  TEXT,
    updated_at  REAL
);

CREATE TABLE IF NOT EXISTS tracks (
    id           TEXT PRIMARY KEY,
    folder_id    TEXT REFERENCES folders(id) ON DELETE CASCADE,
    path         TEXT UNIQUE NOT NULL,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    year         TEXT,
    track_number INTEGER,
    duration_s   INTEGER,
    cover_hash   TEXT,
    file_size    INTEGER,
    mtime        REAL,
    updated_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_tracks_folder ON tracks(folder_id);
CREATE INDEX IF NOT EXISTS idx_tracks_path   ON tracks(path);

CREATE TABLE IF NOT EXISTS download_jobs (
    id           TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    name         TEXT,
    kind         TEXT,
    cover        TEXT,
    track_count  INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'queued',
    landed       INTEGER DEFAULT 0,
    failed       INTEGER DEFAULT 0,
    skipped      INTEGER DEFAULT 0,
    current_track TEXT,
    error        TEXT,
    created_at   REAL,
    started_at   REAL,
    finished_at  REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON download_jobs(status, created_at);
"""


def conn() -> sqlite3.Connection:
    """The calling thread's connection, opened on first use."""
    existing = getattr(_local, "conn", None)
    if existing is not None:
        return existing

    config.ensure_dirs()
    connection = sqlite3.connect(str(config.DB_PATH), timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    _local.conn = connection
    return connection


def init() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        connection = conn()
        connection.executescript(SCHEMA)
        connection.commit()
        _initialised = True
        log.info("Database ready at %s", config.DB_PATH)


def close() -> None:
    """Close this thread's connection (used on worker shutdown)."""
    existing = getattr(_local, "conn", None)
    if existing is not None:
        existing.close()
        _local.conn = None


# ---------------------------------------------------------------- library ---

def upsert_folder(row: Dict[str, Any]) -> None:
    conn().execute(
        """INSERT INTO folders (id, path, name, artist, track_count, duration_s,
                                cover_hash, updated_at)
           VALUES (:id, :path, :name, :artist, :track_count, :duration_s,
                   :cover_hash, :updated_at)
           ON CONFLICT(id) DO UPDATE SET
               path=excluded.path, name=excluded.name, artist=excluded.artist,
               track_count=excluded.track_count, duration_s=excluded.duration_s,
               cover_hash=excluded.cover_hash, updated_at=excluded.updated_at""",
        row,
    )


def ensure_folder(folder_id: str, path: str, name: str) -> None:
    """Create a placeholder folder row so track inserts satisfy the foreign key.

    Aggregates (track_count, duration, cover) are filled in by upsert_folder at
    the end of the scan, once we know what's actually in the directory.
    """
    conn().execute(
        "INSERT OR IGNORE INTO folders (id, path, name, updated_at) VALUES (?, ?, ?, ?)",
        (folder_id, path, name, time.time()),
    )


def upsert_track(row: Dict[str, Any]) -> None:
    conn().execute(
        """INSERT INTO tracks (id, folder_id, path, title, artist, album, year,
                               track_number, duration_s, cover_hash, file_size,
                               mtime, updated_at)
           VALUES (:id, :folder_id, :path, :title, :artist, :album, :year,
                   :track_number, :duration_s, :cover_hash, :file_size,
                   :mtime, :updated_at)
           ON CONFLICT(id) DO UPDATE SET
               folder_id=excluded.folder_id, path=excluded.path,
               title=excluded.title, artist=excluded.artist, album=excluded.album,
               year=excluded.year, track_number=excluded.track_number,
               duration_s=excluded.duration_s, cover_hash=excluded.cover_hash,
               file_size=excluded.file_size, mtime=excluded.mtime,
               updated_at=excluded.updated_at""",
        row,
    )


def known_track_stamps() -> Dict[str, tuple]:
    """path -> (mtime, file_size), used to skip unchanged files on rescan."""
    rows = conn().execute("SELECT path, mtime, file_size FROM tracks").fetchall()
    return {r["path"]: (r["mtime"], r["file_size"]) for r in rows}


def tracks_for_scan() -> Dict[str, sqlite3.Row]:
    """path -> row, with everything a rescan needs for an unchanged file.

    One query instead of a `track_by_path` per file. The old shape cost a
    SQLite round-trip for every track on disk on every tick — imperceptible at
    50 tracks, seconds of avoidable work at a few thousand, on the same core
    that is decoding audio.
    """
    rows = conn().execute(
        "SELECT path, mtime, file_size, duration_s, cover_hash, artist, album FROM tracks"
    ).fetchall()
    return {r["path"]: r for r in rows}


def delete_tracks(paths: Iterable[str]) -> int:
    paths = list(paths)
    if not paths:
        return 0
    connection = conn()
    connection.executemany("DELETE FROM tracks WHERE path = ?", [(p,) for p in paths])
    return len(paths)


def prune_empty_folders() -> int:
    cur = conn().execute(
        "DELETE FROM folders WHERE id NOT IN (SELECT DISTINCT folder_id FROM tracks)"
    )
    return cur.rowcount or 0


def library_tree() -> List[Dict[str, Any]]:
    """The whole index, shaped for GET /api/library."""
    connection = conn()
    folders = connection.execute(
        "SELECT * FROM folders ORDER BY name COLLATE NOCASE"
    ).fetchall()
    # `track_number IS NULL` first in the sort keeps untagged files at the
    # bottom of a folder — SQLite sorts NULL lowest, which otherwise floats
    # every untagged track to the top of the list.
    tracks = connection.execute(
        "SELECT * FROM tracks "
        "ORDER BY folder_id, track_number IS NULL, track_number, title COLLATE NOCASE"
    ).fetchall()

    by_folder: Dict[str, List[sqlite3.Row]] = {}
    for track in tracks:
        by_folder.setdefault(track["folder_id"], []).append(track)

    return [_folder_json(f, by_folder.get(f["id"], [])) for f in folders]


def _folder_json(folder: sqlite3.Row, tracks: List[sqlite3.Row]) -> Dict[str, Any]:
    return {
        "id": folder["id"],
        "name": folder["name"],
        "artist": folder["artist"],
        "track_count": folder["track_count"],
        "duration_s": folder["duration_s"],
        "cover": f"/api/cover/{folder['cover_hash']}.jpg" if folder["cover_hash"] else None,
        "tracks": [
            {
                "id": t["id"],
                "title": t["title"],
                "artist": t["artist"],
                "album": t["album"],
                "year": t["year"],
                "track_number": t["track_number"],
                "duration_s": t["duration_s"],
                "file": "/music/" + t["path"].replace("\\", "/"),
                "cover": f"/api/cover/{t['cover_hash']}.jpg" if t["cover_hash"] else None,
            }
            for t in tracks
        ],
    }


def track_by_path(path: str) -> Optional[sqlite3.Row]:
    return conn().execute("SELECT * FROM tracks WHERE path = ?", (path,)).fetchone()


def counts() -> Dict[str, int]:
    connection = conn()
    return {
        "folders": connection.execute("SELECT COUNT(*) c FROM folders").fetchone()["c"],
        "tracks": connection.execute("SELECT COUNT(*) c FROM tracks").fetchone()["c"],
    }


# ------------------------------------------------------------------- jobs ---

def create_job(job_id: str, url: str, name: str, kind: str,
               cover: Optional[str], track_count: int) -> None:
    conn().execute(
        """INSERT INTO download_jobs (id, url, name, kind, cover, track_count,
                                      status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)""",
        (job_id, url, name, kind, cover, track_count, time.time()),
    )
    conn().commit()


def claim_next_job() -> Optional[sqlite3.Row]:
    """Oldest queued job, atomically flipped to running."""
    connection = conn()
    with connection:
        row = connection.execute(
            "SELECT * FROM download_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "UPDATE download_jobs SET status='running', started_at=? WHERE id=?",
            (time.time(), row["id"]),
        )
    return connection.execute(
        "SELECT * FROM download_jobs WHERE id=?", (row["id"],)
    ).fetchone()


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"name", "kind", "cover", "track_count", "status", "landed", "failed",
               "skipped", "current_track", "error", "started_at", "finished_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    connection = conn()
    with connection:
        connection.execute(
            f"UPDATE download_jobs SET {assignments} WHERE id=?",
            (*fields.values(), job_id),
        )


def get_job(job_id: str) -> Optional[sqlite3.Row]:
    return conn().execute("SELECT * FROM download_jobs WHERE id=?", (job_id,)).fetchone()


def job_status() -> Dict[str, Any]:
    """Shape for GET /api/download/status."""
    connection = conn()
    active_row = connection.execute(
        "SELECT * FROM download_jobs WHERE status IN ('running','queued') "
        "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, created_at LIMIT 1"
    ).fetchone()
    queued = connection.execute(
        "SELECT COUNT(*) c FROM download_jobs WHERE status='queued'"
    ).fetchone()["c"]
    history = connection.execute(
        "SELECT * FROM download_jobs WHERE status IN ('done','failed','cancelled') "
        "ORDER BY finished_at DESC LIMIT 20"
    ).fetchall()

    return {
        "active": job_json(active_row) if active_row else None,
        "queued": queued,
        "history": [job_json(r) for r in history],
    }


def job_json(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "job_id": row["id"],
        "url": row["url"],
        "name": row["name"],
        "kind": row["kind"],
        "cover": row["cover"],
        "total": row["track_count"],
        "status": row["status"],
        "landed": row["landed"],
        "failed": row["failed"],
        "skipped": row["skipped"],
        "current_track": row["current_track"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def reset_orphaned_jobs() -> int:
    """Jobs left 'running' by a crash or power cut are re-queued at boot."""
    connection = conn()
    with connection:
        cur = connection.execute(
            "UPDATE download_jobs SET status='queued', started_at=NULL WHERE status='running'"
        )
    if cur.rowcount:
        log.warning("Re-queued %d job(s) interrupted by shutdown", cur.rowcount)
    return cur.rowcount or 0
