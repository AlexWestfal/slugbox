"""Spotify metadata lookup.

Deliberately separate from `downloader.py`: spotDL refuses to build a
`Downloader` when FFmpeg is missing, but reading metadata needs no FFmpeg at
all. Keeping the preview path on the bare `SpotifyClient` means the UI can show
"here's what you're about to download" on a box where FFmpeg isn't installed
yet, and surface that as a clear warning instead of a traceback.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from . import config
from .logging_setup import get_logger

log = get_logger("spotify")

_init_lock = threading.Lock()
_initialised = False

# The UI previews a link and then immediately enqueues the same link, so
# without this every download costs two full metadata round trips — about ten
# seconds each for a 25-track album, during which the DOWNLOAD button looks
# dead. Small and short-lived: this is a latency cache, not a store.
_PREVIEW_TTL_S = 300
_PREVIEW_MAX = 16
_preview_cache: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_get(url: str) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        hit = _preview_cache.get(url)
        if hit is None:
            return None
        stamp, payload = hit
        if time.time() - stamp > _PREVIEW_TTL_S:
            _preview_cache.pop(url, None)
            return None
        _preview_cache.move_to_end(url)
        return payload


def _cache_put(url: str, payload: Dict[str, Any]) -> None:
    with _cache_lock:
        _preview_cache[url] = (time.time(), payload)
        _preview_cache.move_to_end(url)
        while len(_preview_cache) > _PREVIEW_MAX:
            _preview_cache.popitem(last=False)


def invalidate_preview(url: str) -> None:
    with _cache_lock:
        _preview_cache.pop(url.strip(), None)


class SpotifyError(Exception):
    """Anything that stopped us resolving a URL to a track list."""


class InvalidURL(SpotifyError):
    pass


# Accepts open.spotify.com links (with or without locale segment or query
# string), spotify: URIs, and bare share links.
_URL_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist|artist)/|"
    r"spotify:(track|album|playlist|artist):)"
    r"([A-Za-z0-9]{16,40})",
    re.IGNORECASE,
)

_SUPPORTED_KINDS = ("track", "album", "playlist", "artist")


def detect_url_type(url: str) -> Tuple[str, str]:
    """Return (kind, spotify_id). Raises InvalidURL if it isn't a Spotify link."""
    if not url or not isinstance(url, str):
        raise InvalidURL("No URL supplied.")
    match = _URL_RE.search(url.strip())
    if not match:
        raise InvalidURL("That doesn't look like a Spotify track, album or playlist link.")
    kind = (match.group(1) or match.group(2) or "").lower()
    if kind not in _SUPPORTED_KINDS:
        raise InvalidURL(f"Unsupported Spotify link type: {kind}")
    return kind, match.group(3)


def _ensure_client() -> None:
    """Initialise spotDL's Spotify client exactly once per process."""
    global _initialised
    with _init_lock:
        if _initialised:
            return
        from spotdl.utils.config import DEFAULT_CONFIG
        from spotdl.utils.spotify import SpotifyClient

        cfg = config.load()
        client_id = cfg.get("spotify_client_id") or DEFAULT_CONFIG["client_id"]
        client_secret = cfg.get("spotify_client_secret") or DEFAULT_CONFIG["client_secret"]

        try:
            SpotifyClient.init(
                client_id=client_id,
                client_secret=client_secret,
                user_auth=False,
                no_cache=True,
            )
        except Exception as exc:  # already-initialised is fine; anything else isn't
            if "already" not in str(exc).lower():
                raise SpotifyError(f"Could not initialise Spotify client: {exc}") from exc
        _initialised = True


def song_to_dict(song: Any) -> Dict[str, Any]:
    """Flatten a spotDL Song into the shape the API and UI speak."""
    return {
        "title": song.name,
        "artist": song.artist or (song.artists[0] if song.artists else ""),
        "artists": list(song.artists or []),
        "album": song.album_name,
        "album_artist": song.album_artist,
        "year": str(song.year) if song.year else None,
        "track_number": song.track_number,
        "duration_s": int(song.duration or 0),
        "cover": song.cover_url,
        "spotify_id": song.song_id,
        "url": song.url,
    }


def fetch_songs(url: str) -> List[Any]:
    """Resolve a Spotify URL to spotDL Song objects. One network round trip."""
    detect_url_type(url)          # fail fast on junk before touching the network
    _ensure_client()

    from spotdl.utils.search import get_simple_songs

    try:
        songs = get_simple_songs(
            [url.strip()],
            playlist_numbering=bool(config.get("playlist_numbering")),
        )
    except Exception as exc:
        log.warning("Metadata lookup failed for %s: %s", url, exc)
        raise SpotifyError(f"Could not read that link: {exc}") from exc

    if not songs:
        raise SpotifyError("Spotify returned no tracks for that link.")
    return songs


def preview(url: str) -> Dict[str, Any]:
    """Metadata for the confirmation card, before any job is created.

    Cached briefly so the preview-then-confirm flow costs one round trip, not
    two. `fetch_songs` is deliberately uncached — the download must always work
    from freshly resolved Song objects.
    """
    url = url.strip()
    cached = _cache_get(url)
    if cached is not None:
        log.debug("Preview cache hit for %s", url)
        return cached

    kind, spotify_id = detect_url_type(url)
    songs = fetch_songs(url)
    first = songs[0]

    # For a single track there is no list; fall back to the track's own album.
    name = first.list_name or (first.name if kind == "track" else first.album_name)

    payload = {
        "kind": kind,
        "spotify_id": spotify_id,
        "name": name,
        "artist": _folder_artist(songs),
        "track_count": len(songs),
        "cover": first.cover_url,
        "duration_s": sum(int(s.duration or 0) for s in songs),
        "tracks": [song_to_dict(s) for s in songs],
    }
    _cache_put(url, payload)
    return payload


def _folder_artist(songs: List[Any]) -> Optional[str]:
    """One artist if the whole set shares one, else 'Various Artists'."""
    names = {(s.album_artist or s.artist or "").strip() for s in songs}
    names.discard("")
    if not names:
        return None
    return next(iter(names)) if len(names) == 1 else "Various Artists"
