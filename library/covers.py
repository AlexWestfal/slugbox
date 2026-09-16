"""Cover art extraction and caching.

Art is embedded in the audio files themselves, in a different place for every
container. We pull the bytes out once, key them by content hash, and write a
single copy to `data/.cache/covers/`. Identical art across a 14-track album
collapses to one file on disk and one HTTP cache entry in Chromium.

Hash is SHA-256 truncated to 16 hex chars — 64 bits of collision resistance
against a few thousand images is ample, and it keeps filenames short.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Tuple

from engine import config
from engine.logging_setup import get_logger

log = get_logger("covers")

# Magic numbers, so we can name the cached file correctly regardless of what
# the tag claims its MIME type is.
_MAGIC: Tuple[Tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)


def detect_image_ext(data: bytes) -> str:
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return "jpg"


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def extract_cover_bytes(audio) -> Optional[bytes]:
    """Pull embedded art out of an already-open Mutagen file object.

    Every container hides it somewhere different:
      MP3/WAV/AIFF  ID3 APIC frames
      MP4/M4A       the 'covr' atom
      FLAC          a native picture block
      OGG/Opus      base64 METADATA_BLOCK_PICTURE in the Vorbis comments
    """
    if audio is None:
        return None

    try:
        # FLAC (and anything else exposing native picture blocks)
        pictures = getattr(audio, "pictures", None)
        if pictures:
            return bytes(pictures[0].data)

        tags = getattr(audio, "tags", None)
        if tags is None:
            return None

        # ID3 — MP3, and ID3-in-WAV/AIFF
        getall = getattr(tags, "getall", None)
        if callable(getall):
            frames = getall("APIC")
            if frames:
                return bytes(frames[0].data)

        # MP4 / M4A
        if "covr" in tags:
            covers = tags["covr"]
            if covers:
                return bytes(covers[0])

        # Vorbis comments (OGG, Opus)
        if "metadata_block_picture" in tags:
            import base64
            from mutagen.flac import Picture

            raw = tags["metadata_block_picture"]
            encoded = raw[0] if isinstance(raw, list) else raw
            return bytes(Picture(base64.b64decode(encoded)).data)

        # Some taggers stash a raw image under coverart/
        if "coverart" in tags:
            import base64

            raw = tags["coverart"]
            encoded = raw[0] if isinstance(raw, list) else raw
            return base64.b64decode(encoded)

    except Exception as exc:
        # Corrupt art is common in the wild and must never abort a scan.
        log.debug("Cover extraction failed: %s", exc)

    return None


def cache_cover(data: Optional[bytes]) -> Optional[str]:
    """Write art to the cache if new. Returns its hash, or None."""
    if not data or len(data) < 64:
        return None

    digest = hash_bytes(data)
    ext = detect_image_ext(data)
    target = cover_path(digest)

    if target.exists():
        return digest

    try:
        config.COVER_DIR.mkdir(parents=True, exist_ok=True)
        # Written .jpg regardless of true format: browsers sniff content, and a
        # single extension keeps the /api/cover/<hash>.jpg route trivial. The
        # real type is recorded alongside for the Content-Type header.
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        if ext != "jpg":
            target.with_suffix(".type").write_text(ext, encoding="utf-8")
    except OSError as exc:
        log.warning("Could not cache cover %s: %s", digest, exc)
        return None

    return digest


def cover_path(digest: str) -> Path:
    return config.COVER_DIR / f"{digest}.jpg"


def cover_mimetype(digest: str) -> str:
    marker = cover_path(digest).with_suffix(".type")
    if marker.exists():
        try:
            ext = marker.read_text(encoding="utf-8").strip()
            return {"png": "image/png", "gif": "image/gif",
                    "bmp": "image/bmp", "webp": "image/webp"}.get(ext, "image/jpeg")
        except OSError:
            pass
    return "image/jpeg"


def is_valid_hash(digest: str) -> bool:
    """Guard the cover route against path traversal."""
    return bool(digest) and len(digest) == 16 and all(c in "0123456789abcdef" for c in digest)
