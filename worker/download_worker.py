"""Job queue consumer.

One job at a time, by design. The Pi is simultaneously serving audio to the
kiosk browser, and two playlists transcoding in parallel is what makes playback
stutter. Per-track parallelism inside a job is capped separately by the
`threads` setting (2 on a Pi 4).
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

from engine import config, spotify_api
from engine.downloader import Callbacks, EngineError, FFmpegMissing, engine
from engine.logging_setup import get_logger
from library import db

log = get_logger("worker")

# The WebSocket is a firehose during a download — one update per yt-dlp chunk.
# Chromium on a Pi does not need 40 messages a second to draw a progress bar.
_PROGRESS_THROTTLE_S = 0.4


class DownloadWorker(threading.Thread):
    def __init__(self, broadcast: Callable[[dict], None],
                 nudge_scanner: Callable[[], None]) -> None:
        super().__init__(name="download-worker", daemon=True)
        self._broadcast = broadcast
        self._nudge_scanner = nudge_scanner
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cancel = threading.Event()
        self._current_job_id: Optional[str] = None
        self._lock = threading.Lock()

    # ---- public API ---------------------------------------------------

    def enqueue(self, url: str, preview: Dict[str, Any]) -> str:
        """Create a queued job from an already-fetched preview."""
        job_id = str(uuid.uuid4())
        db.create_job(
            job_id=job_id,
            url=url,
            name=preview.get("name") or "Unknown",
            kind=preview.get("kind") or "playlist",
            cover=preview.get("cover"),
            track_count=int(preview.get("track_count") or 0),
        )
        log.info("Queued job %s — %s (%d tracks)", job_id[:8],
                 preview.get("name"), preview.get("track_count") or 0)
        self._broadcast({"event": "download_queued", "job_id": job_id,
                         "name": preview.get("name"),
                         "total": preview.get("track_count") or 0,
                         "cover": preview.get("cover")})
        self._wake.set()
        return job_id

    def cancel(self, job_id: str) -> bool:
        """Cancel a running job, or drop a queued one outright."""
        with self._lock:
            running = self._current_job_id
        if running == job_id:
            self._cancel.set()
            log.info("Cancel requested for running job %s", job_id[:8])
            return True

        row = db.get_job(job_id)
        if row is not None and row["status"] == "queued":
            db.update_job(job_id, status="cancelled", finished_at=time.time())
            self._broadcast({"event": "download_complete", "job_id": job_id,
                             "summary": {"cancelled": True, "landed": 0,
                                         "failed": 0, "skipped": 0}})
            return True
        return False

    def stop(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._wake.set()

    @property
    def current_job_id(self) -> Optional[str]:
        with self._lock:
            return self._current_job_id

    # ---- the loop -----------------------------------------------------

    def run(self) -> None:
        db.init()
        db.reset_orphaned_jobs()

        while not self._stop.is_set():
            job = db.claim_next_job()
            if job is None:
                self._wake.wait(timeout=3.0)
                self._wake.clear()
                continue

            with self._lock:
                self._current_job_id = job["id"]
            self._cancel.clear()

            try:
                self._run_job(job)
            except Exception:
                log.exception("Job %s crashed", job["id"][:8])
                db.update_job(job["id"], status="failed",
                              error="Internal error — see logs",
                              finished_at=time.time())
            finally:
                with self._lock:
                    self._current_job_id = None

        db.close()

    def _run_job(self, job) -> None:
        job_id = job["id"]
        url = job["url"]
        log.info("Starting job %s — %s", job_id[:8], job["name"])

        tally = {"landed": 0, "failed": 0, "skipped": 0}
        last_push = {"at": 0.0}

        self._broadcast({"event": "download_started", "job_id": job_id,
                         "name": job["name"], "total": job["track_count"],
                         "cover": job["cover"]})

        def push_progress(current: Optional[str] = None, force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - last_push["at"] < _PROGRESS_THROTTLE_S:
                return
            last_push["at"] = now
            self._broadcast({
                "event": "download_progress",
                "job_id": job_id,
                "landed": tally["landed"],
                "failed": tally["failed"],
                "skipped": tally["skipped"],
                "total": job["track_count"],
                "current": current,
            })

        def on_track_started(track: dict) -> None:
            label = f"{track['title']} - {track['artist']}".strip(" -")
            db.update_job(job_id, current_track=label)
            push_progress(label)

        def on_track_progress(track: dict, pct: int, message: str) -> None:
            label = f"{track['title']} - {track['artist']}".strip(" -")
            self._maybe_push_track(job_id, track, pct, message, last_push)

        def on_track_done(track: dict, path: str) -> None:
            tally["landed"] += 1
            db.update_job(job_id, landed=tally["landed"])
            self._broadcast({"event": "track_landed",
                             "folder": track.get("album"),
                             "job_id": job_id,
                             "track": track, "path": path})
            # The scanner owns the index; tell it there's something new rather
            # than writing the row from here and risking two writers disagreeing.
            self._nudge_scanner()
            push_progress(force=True)

        def on_track_skipped(track: dict, path: str) -> None:
            tally["skipped"] += 1
            db.update_job(job_id, skipped=tally["skipped"])
            push_progress()

        def on_track_failed(title: str, error: str) -> None:
            tally["failed"] += 1
            db.update_job(job_id, failed=tally["failed"])
            log.warning("Track failed (%s): %s", title, error)
            self._broadcast({"event": "track_failed", "job_id": job_id,
                             "title": title, "error": error})
            push_progress(force=True)

        callbacks = Callbacks(
            on_track_started=on_track_started,
            on_track_progress=on_track_progress,
            on_track_done=on_track_done,
            on_track_skipped=on_track_skipped,
            on_track_failed=on_track_failed,
            on_progress=lambda _pct: push_progress(),
            on_completed=lambda _summary: None,
        )

        try:
            summary = engine.run(url, callbacks, cancel=self._cancel)
        except FFmpegMissing as exc:
            db.update_job(job_id, status="failed", error=str(exc),
                          finished_at=time.time())
            self._broadcast({"event": "download_failed", "job_id": job_id,
                             "error": str(exc)})
            log.error("Job %s failed: %s", job_id[:8], exc)
            return
        except (EngineError, spotify_api.SpotifyError) as exc:
            db.update_job(job_id, status="failed", error=str(exc),
                          finished_at=time.time())
            self._broadcast({"event": "download_failed", "job_id": job_id,
                             "error": str(exc)})
            log.error("Job %s failed: %s", job_id[:8], exc)
            return

        status = "cancelled" if summary.get("cancelled") else "done"
        db.update_job(job_id, status=status, landed=summary["landed"],
                      failed=summary["failed"], skipped=summary["skipped"],
                      current_track=None, finished_at=time.time())

        log.info("Job %s %s — %d landed, %d skipped, %d failed",
                 job_id[:8], status, summary["landed"],
                 summary["skipped"], summary["failed"])

        self._nudge_scanner()
        self._broadcast({"event": "download_complete", "job_id": job_id,
                         "summary": summary})

    def _maybe_push_track(self, job_id: str, track: dict, pct: int,
                          message: str, last_push: Dict[str, float]) -> None:
        now = time.monotonic()
        if now - last_push["at"] < _PROGRESS_THROTTLE_S:
            return
        last_push["at"] = now
        self._broadcast({
            "event": "track_progress",
            "job_id": job_id,
            "title": track.get("title"),
            "artist": track.get("artist"),
            "percent": pct,
            "stage": message,
        })
