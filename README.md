# slugbox

A local jukebox appliance for a Raspberry Pi 4. One screen, no page navigation,
no build step.

Three parts in one Python project:

1. **Web server** — Flask, serves the kiosk frontend, a REST API, audio with
   Range support, and a WebSocket event stream
2. **Library scanner** — walks the music directory, reads tags with Mutagen,
   indexes into SQLite, extracts and caches cover art
3. **Download worker** — consumes a job queue, driving **spotDL** to fetch and
   tag Spotify playlists in the background

The frontend never streams from the internet. Every track it plays is a tagged
file on local disk.

---

## Quick start

### On the Pi

```bash
git clone <this repo> ~/slugbox && cd ~/slugbox
./install.sh --kiosk
```

That installs system packages (including FFmpeg), builds a venv, writes a
default `config.json`, installs and starts the systemd unit, and sets Chromium
to open the player full-screen on boot.

### Anywhere, for development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
```

Then open <http://localhost:5000>.

### Without a backend at all

Open `static/index.html` straight off disk. The player detects that no API is
reachable, shows a `DEMO` chip, and falls back to a hardcoded library with
generated cover art, a virtual transport, and a simulated download pipeline.
Useful for working on the UI; it downloads nothing.

---

## Engine choice: spotDL, not Sunnify

The original brief specified forking the Sunnify CLI engine, describing it as
"MIT-style." It isn't. Sunnify 2.4.2 ships a custom licence whose **first
condition forbids modifying any portion of the software**, with modification
inquiries directed to the author. That is confirmed in three places in their
repo: `LICENSE`, `pyproject.toml` (`license = "Custom - Educational Use Only"`),
and `README.md`. Forking, de-Qt-ing and rebranding it is exactly what that
condition prohibits.

This build uses [spotDL](https://github.com/spotDL/spotify-downloader) instead,
which is MIT licensed and permits modification and redistribution. It runs the
same pipeline — Spotify metadata, YouTube Music matching, yt-dlp fetch, FFmpeg
transcode, Mutagen tagging — and exposes a Python API, so it sits behind the
callback protocol the brief defined without any of the licence friction.

No Sunnify code is present in this repository.

---

## Architecture

```
server.py                Flask app, routes, WebSocket hub, thread startup
engine/
  config.py              settings registry, atomic JSON persistence, paths
  logging_setup.py       rotating file log + crash hooks
  spotify_api.py         URL parsing and metadata preview (no FFmpeg needed)
  downloader.py          spotDL driven headlessly behind the callbacks
library/
  db.py                  SQLite schema, WAL, library queries, job queue
  covers.py              embedded art extraction, content-hash cache
  scanner.py             stat-first filesystem walker and indexer
worker/
  download_worker.py     job queue consumer, wires engine -> DB -> WebSocket
static/index.html        the whole frontend, self-contained
systemd/slugbox.service  unit template (install.sh fills in user and paths)
data/                    created at runtime: library.db, .cache/covers/, logs/
```

### Callback protocol

`engine/downloader.py` exposes the engine as six callbacks, so the worker never
imports spotDL and the engine never imports Flask:

```python
on_track_started(track)             # a track began
on_track_progress(track, pct, msg)  # fine-grained, many times per track
on_track_done(track, path)          # landed on disk
on_track_skipped(track, path)       # already present, left alone
on_track_failed(title, error)       # no match, or the download failed
on_progress(pct)                    # whole-job percentage
on_completed(summary)               # final tallies
```

Swapping engines means rewriting one file.

---

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/library` | full index, shape below |
| GET | `/api/cover/<hash>.jpg` | cached art; 16-hex-char hashes only |
| GET | `/music/<path>` | audio, **supports Range** so seeking works |
| GET | `/api/preview?url=` | metadata only, creates no job, no FFmpeg needed |
| POST | `/api/download` | `{"url": "..."}` → `{job_id, name, track_count, cover}` |
| GET | `/api/download/status` | `{active, queued, history[]}` |
| POST | `/api/download/<job_id>/cancel` | cancels running or queued |
| GET/POST | `/api/settings` | reads/writes the same `config.json` the engine uses |
| POST | `/api/rescan` | force a library rescan |
| GET | `/api/health` | version, counts, FFmpeg status, active job |
| WS | `/ws` | event stream, below |

`/api/library` returns:

```json
{"folders": [{
  "id": "25c2e8c7a3787e47",
  "name": "Golden Hour Sessions",
  "artist": "Ruby Lane Trio",
  "track_count": 4,
  "duration_s": 43,
  "cover": "/api/cover/8e3dce08d83735b5.jpg",
  "tracks": [{
    "id": "15f9ebea691f8b3b", "title": "Ruby Lane", "artist": "Ruby Lane Trio",
    "album": "Golden Hour Sessions", "year": "2021", "track_number": 1,
    "duration_s": 12, "file": "/music/Golden Hour Sessions/01. Ruby Lane.wav",
    "cover": "/api/cover/8e3dce08d83735b5.jpg"
  }]
}]}
```

`API.normalizeLibrary()` in `static/index.html` is the only place that shape is
consumed. Change it there and nowhere else.

### WebSocket events

```json
{"event": "download_queued",   "job_id": "...", "name": "...", "total": 25}
{"event": "download_started",  "job_id": "...", "name": "...", "total": 25}
{"event": "download_progress", "job_id": "...", "landed": 19, "total": 25, "current": "..."}
{"event": "track_progress",    "job_id": "...", "title": "...", "percent": 62, "stage": "Downloading"}
{"event": "track_landed",      "job_id": "...", "folder": "...", "track": {...}}
{"event": "track_failed",      "job_id": "...", "title": "...", "error": "..."}
{"event": "download_complete", "job_id": "...", "summary": {"landed": 24, "failed": 1, "skipped": 0}}
{"event": "download_failed",   "job_id": "...", "error": "..."}
{"event": "library_updated",   "added": 24}
```

Unknown events are ignored by the client on purpose, so the server can add new
ones without breaking an older kiosk.

**Deviation from the brief:** it asked for Flask-SocketIO. This uses
`flask-sock` (plain WebSocket) instead, because the documented contract — path
`/ws`, bare JSON frames — *is* plain WebSocket, not Socket.IO, which uses its
own framing at `/socket.io`. Plain WebSocket also means the frontend uses the
native browser API and ships no client library, which matters when the whole UI
is one self-contained file.

---

## How the pieces interact

The scanner owns the library index. When a download lands a file, the worker
does **not** write the track row — it nudges the scanner, which reads the real
tags off disk and emits `library_updated`. One writer, one source of truth, and
the index always reflects what is actually on the disk rather than what the
downloader believed it wrote.

SQLite runs in WAL mode because three things touch it concurrently (Flask
request threads reading, scanner writing, worker writing). In the default
rollback journal a single writer blocks every reader, which shows up as the
kiosk UI stalling mid-scan. Connections are thread-local.

---

## Configuration

`config.json`, next to `server.py`. Every key, its type and its default live in
`SETTINGS` in `engine/config.py`; `/api/settings` serves that registry so the UI
can render a form from it.

| Key | Default | Notes |
| --- | --- | --- |
| `music_dir` | `~/Music` | scanned, and written to by downloads |
| `format` | `mp3` | mp3, m4a, flac, opus, ogg, wav |
| `bitrate` | `320k` | or `auto` to match the source |
| `threads` | `2` | per-track parallelism — see below |
| `output_template` | `{list-name}/{track-number}. {title} - {artists}.{output-ext}` | spotDL template |
| `scan_interval_s` | `8` | library poll interval |
| `port` | `5000` | |
| `ffmpeg` | `ffmpeg` | path, or `ffmpeg` to use `PATH` |
| `spotify_client_id` / `_secret` | blank | blank uses spotDL's built-in public credentials |

A corrupt `config.json` logs an error and falls back to defaults rather than
refusing to boot. It is read as `utf-8-sig`, so a BOM is tolerated.

---

## Pi-specific notes

- **`threads: 2`.** Four parallel FFmpeg transcodes saturate the Pi 4's CPU and
  make playback in Chromium stutter.
- **One job at a time.** Per-track parallelism is capped by `threads`; whole
  playlists never run concurrently.
- **Polling, not inotify.** The music root is often a USB stick or a network
  mount, where inotify silently does nothing. A rescan stats every file and only
  re-reads tags for files whose mtime or size changed.
- **FFmpeg is required for downloads**, not for playback. Without it the library
  plays fine and `POST /api/download` returns 503 with an explanation; the
  settings panel shows the warning.
- **Autoplay.** The kiosk launcher passes
  `--autoplay-policy=no-user-gesture-required`; without it the first `play()`
  after boot is blocked.
- Jobs left `running` by a power cut are re-queued at startup.
- When YouTube changes and downloads start failing,
  `.venv/bin/pip install -U yt-dlp` is usually the entire fix.

---

## Frontend

`static/index.html` is the whole player — inlined CSS and JS, no framework, no
bundler. Design notes and controls are in the file's own header comments.

Keyboard: Space/K play-pause · ←/→ seek · ↑/↓ volume · N next · P previous ·
D download panel · Esc close/back. Media keys work via the Media Session API.

Fonts come from Google Fonts. An offline Pi falls back to Arial Narrow / Segoe
UI / DejaVu Sans Mono — legible, but the period character is lost. To fix,
download Oswald, Rubik and IBM Plex Mono into `static/` and swap the `<link>`
for local `@font-face` rules.

---

## Verified

Against a generated fixture library of tagged WAV files (real RIFF audio, real
ID3 APIC frames) plus live Spotify metadata:

- 23/23 API tests pass — library shape, Range requests (`206` + correct
  `Content-Range`), cover cache, settings round-trip, error paths
- Path traversal blocked on `/music` and `/api/cover` (`../`, URL-encoded, and
  `....//` variants)
- Scanner: corrupt file skipped without crashing, non-audio ignored, covers
  deduplicated by content hash, `Various Artists` derived from mixed tags,
  loose files bucketed into `Singles`, untagged files sorted last
- Live playback through the real backend, including seek and auto-advance to
  the next track
- Live Spotify preview end to end through the UI
- Real download, end to end: Spotify track and album downloads verified,
  transcoded to tagged MP3s with embedded art and indexed by the scanner
