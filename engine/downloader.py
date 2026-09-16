"""The download engine: spotDL driven headlessly behind a callback protocol.

spotDL (MIT) does the heavy lifting — Spotify metadata, YouTube Music matching,
yt-dlp fetch, FFmpeg transcode, Mutagen tagging. This module's whole job is to
drive it without a terminal UI and turn its progress stream into the six
callbacks the download worker needs:

    on_track_started(track)            a track began
    on_track_progress(track, pct, msg) fine-grained, several times per track
    on_track_done(track, path)         landed on disk
    on_track_skipped(track, path)      already present, left alone
    on_track_failed(title, error)      no match, or the download blew up
    on_progress(pct)                   whole-job percentage
    on_completed(summary)              final tallies

Cancellation is checked between batches rather than mid-track: spotDL's pool
runs an asyncio gather to completion, so the finest safe granularity is one
batch of `threads` tracks.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, spotify_api
from .logging_setup import get_logger

log = get_logger("engine")


def _noop(*_args: Any, **_kwargs: Any) -> None:
    pass


@dataclass
class Callbacks:
    """Every hook is optional; unset ones are no-ops."""
    on_track_started: Callable[[dict], None] = _noop
    on_track_progress: Callable[[dict, int, str], None] = _noop
    on_track_done: Callable[[dict, str], None] = _noop
    on_track_skipped: Callable[[dict, str], None] = _noop
    on_track_failed: Callable[[str, str], None] = _noop
    on_progress: Callable[[int], None] = _noop
    on_completed: Callable[[dict], None] = _noop


class EngineError(Exception):
    pass


class FFmpegMissing(EngineError):
    """spotDL will not construct a Downloader without FFmpeg."""


# spotDL status strings, from spotdl/download/progress_handler.py. Anything not
# listed here is treated as ordinary in-flight progress.
_STATUS_SKIPPED = "Skipped"
_STATUS_ERROR = "Error"
_STATUS_DONE = "Done"


@dataclass
class _Outcome:
    """What actually happened to one track, assembled as events arrive."""
    song: Any
    status: str = ""
    path: Optional[str] = None
    error: Optional[str] = None
    started: bool = False
    settled: bool = False      # terminal callback already fired for this track


class DownloadEngine:
    """Reusable across jobs — the spotDL Downloader and its event loop are
    expensive to build, so we keep one alive and rebuild only when the
    settings that matter actually change."""

    def __init__(self) -> None:
        self._downloader: Any = None
        self._fingerprint: Optional[tuple] = None
        self._lock = threading.Lock()

    # ---- settings ----------------------------------------------------

    @staticmethod
    def _build_settings() -> Dict[str, Any]:
        cfg = config.load()
        ffmpeg = config.ffmpeg_path()
        if not ffmpeg:
            raise FFmpegMissing(
                "FFmpeg was not found. Install it (`sudo apt install ffmpeg`) "
                "or set the `ffmpeg` path in settings."
            )

        template = str(cfg["output_template"]).lstrip("/\\")
        output = str(config.music_dir() / template)

        return {
            "output": output,
            "format": cfg["format"],
            "bitrate": None if cfg["bitrate"] == "auto" else cfg["bitrate"],
            "threads": max(1, int(cfg["threads"])),
            "ffmpeg": ffmpeg,
            "overwrite": "skip",          # gives us free "already have it" detection
            "playlist_numbering": bool(cfg["playlist_numbering"]),
            "simple_tui": True,           # no rich progress bars into the journal
            "print_errors": False,
            "log_level": "ERROR",
            "scan_for_songs": False,
            "generate_lrc": False,
            "sponsor_block": False,
            "save_errors": None,
        }

    def _fingerprint_of(self, settings: Dict[str, Any]) -> tuple:
        return tuple(sorted(
            (k, str(v)) for k, v in settings.items()
            if k in ("output", "format", "bitrate", "threads", "ffmpeg", "playlist_numbering")
        ))

    def _get_downloader(self) -> Any:
        """Build (or reuse) the spotDL Downloader. Raises FFmpegMissing."""
        settings = self._build_settings()
        fingerprint = self._fingerprint_of(settings)

        with self._lock:
            if self._downloader is not None and fingerprint == self._fingerprint:
                return self._downloader

            from spotdl.download.downloader import Downloader
            from spotdl.download.downloader import DownloaderError

            spotify_api._ensure_client()   # Downloader expects a live Spotify client
            try:
                self._downloader = Downloader(settings=settings)
            except DownloaderError as exc:
                if "ffmpeg" in str(exc).lower():
                    raise FFmpegMissing(str(exc)) from exc
                raise EngineError(str(exc)) from exc

            self._fingerprint = fingerprint
            log.info("spotDL downloader ready — format=%s bitrate=%s threads=%s",
                     settings["format"], settings["bitrate"], settings["threads"])
            return self._downloader

    # ---- the run -----------------------------------------------------

    def run(self, url: str, callbacks: Callbacks,
            cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """Download everything behind `url`. Blocking; call from a worker thread."""
        cancel = cancel or threading.Event()

        songs = spotify_api.fetch_songs(url)
        total = len(songs)
        log.info("Job resolved to %d track(s)", total)

        downloader = self._get_downloader()

        outcomes: Dict[str, _Outcome] = {s.url: _Outcome(song=s) for s in songs}
        state_lock = threading.Lock()
        counters = {"finished": 0}

        def update_callback(tracker: Any, message: str) -> None:
            """Called by spotDL on every progress tick, from its worker threads."""
            song = getattr(tracker, "song", None)
            if song is None:
                return
            outcome = outcomes.get(song.url)
            if outcome is None:
                return

            meta = spotify_api.song_to_dict(song)
            with state_lock:
                first_touch = not outcome.started
                outcome.started = True
                outcome.status = message
                path = getattr(tracker, "path", None)
                if path:
                    outcome.path = str(path)
                # Terminal states must fire exactly once, and from here rather
                # than at the end of the job — otherwise a 25-track album shows
                # "0 landed" until the whole thing finishes and nothing reaches
                # the library until then.
                terminal = message in (_STATUS_DONE, _STATUS_SKIPPED, _STATUS_ERROR)
                first_terminal = terminal and not outcome.settled
                if first_terminal:
                    outcome.settled = True

            if first_touch:
                _safe(callbacks.on_track_started, meta)

            pct = int(getattr(tracker, "progress", 0) or 0)
            _safe(callbacks.on_track_progress, meta, max(0, min(100, pct)), message)

            if first_terminal:
                self._emit_outcome(outcome, meta, callbacks)

        downloader.progress_handler.update_callback = update_callback

        # Batch so cancellation has somewhere to land. spotDL's own semaphore
        # still governs concurrency inside each batch.
        batch_size = max(1, int(config.get("threads")) * 2)
        results: List[Tuple[Any, Optional[Path]]] = []
        cancelled = False

        for start in range(0, total, batch_size):
            if cancel.is_set():
                cancelled = True
                log.info("Job cancelled after %d/%d tracks", counters["finished"], total)
                break

            batch = songs[start:start + batch_size]
            try:
                results.extend(downloader.download_multiple_songs(batch))
            except Exception as exc:
                # One bad batch shouldn't abandon the rest of the playlist.
                log.exception("Batch failed: %s", exc)
                for song in batch:
                    outcomes[song.url].error = str(exc)

            counters["finished"] = min(total, start + len(batch))
            _safe(callbacks.on_progress, int(counters["finished"] / total * 100) if total else 100)

        # spotDL's return value is authoritative for where the file ended up;
        # the status stream tells us whether it was fetched or already there.
        for song, path in results:
            outcome = outcomes.get(song.url)
            if outcome is None:
                continue
            if path is not None:
                outcome.path = str(path)

        summary = self._finalise(outcomes, callbacks, cancelled, total)
        _safe(callbacks.on_completed, summary)
        return summary

    @staticmethod
    def _emit_outcome(outcome: _Outcome, meta: Dict[str, Any],
                      callbacks: Callbacks) -> str:
        """Fire the one terminal callback for a track. Returns its category."""
        title = f"{meta['title']} - {meta['artist']}".strip(" -")
        category = _classify(outcome)

        if category == "skipped":
            _safe(callbacks.on_track_skipped, meta, outcome.path or "")
        elif category == "landed":
            _safe(callbacks.on_track_done, meta, outcome.path or "")
        else:
            reason = outcome.error or (
                "Download error" if outcome.status == _STATUS_ERROR
                else "No matching audio found")
            _safe(callbacks.on_track_failed, title, reason)
        return category

    def _finalise(self, outcomes: Dict[str, _Outcome], callbacks: Callbacks,
                  cancelled: bool, total: int) -> Dict[str, Any]:
        """Tally the run, emitting for any track that never reached a terminal
        status (a batch that raised, say) so the counts always add up."""
        tally = {"landed": 0, "skipped": 0, "failed": 0}

        for outcome in outcomes.values():
            if not outcome.started and cancelled:
                continue                      # never attempted — not a failure

            meta = spotify_api.song_to_dict(outcome.song)
            if not outcome.settled:
                outcome.settled = True
                category = self._emit_outcome(outcome, meta, callbacks)
            else:
                category = _classify(outcome)   # already reported live
            tally[category] += 1

        return {
            "total": total,
            "landed": tally["landed"],
            "skipped": tally["skipped"],
            "failed": tally["failed"],
            "cancelled": cancelled,
        }


def _classify(outcome: _Outcome) -> str:
    """One place decides landed / skipped / failed.

    spotDL's "Done" status is authoritative even when the tracker hasn't had
    set_path() called on it yet, which is why status is checked before path.
    """
    if outcome.status == _STATUS_SKIPPED:
        return "skipped"
    if outcome.status == _STATUS_ERROR:
        return "failed"
    if outcome.status == _STATUS_DONE:
        return "landed"
    return "landed" if outcome.path else "failed"


def _safe(fn: Callable[..., None], *args: Any) -> None:
    """A throwing callback must never take the download down with it."""
    try:
        fn(*args)
    except Exception:
        log.exception("Callback %s raised", getattr(fn, "__name__", fn))


# One engine per process; the worker owns it.
engine = DownloadEngine()
