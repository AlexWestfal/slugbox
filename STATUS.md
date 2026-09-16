# slugbox — status

Last updated: 2026-09-16

A local jukebox appliance for a Raspberry Pi 4: a retro-styled web player in
Chromium kiosk mode, backed by a Python server that indexes local music and
downloads Spotify playlists in the background.

**Current state: working end to end.** Paste a Spotify link, it downloads,
tags, and appears in the player. Verified on Windows with real Spotify links.

---

## 1. Frontend — `static/index.html`

One self-contained file. Inlined CSS and JS, no framework, no bundler, no build
step. Loads directly in Chromium.

### Design
Analog jukebox rather than digital skeuomorphism:
- Warm brown-black palette, amber lamp-light, aged cream. No pure black/white,
  no blue-tinted grays.
- Library folders render as sleeves with **cream jukebox title strips** — title
  in condensed caps, artist in the red those strips actually used. The playing
  folder's strip lights up amber.
- **Radio-dial timeline** with tick marks and a glowing needle.
- Transport buttons with a real 3px press offset; volume fader with a knurled
  metal cap.
- Segmented VU meters in the faceplate; 3-bar equaliser on the playing row.
- Empty state: a glowing record waiting for a coin.
- Fonts: Oswald (display), Rubik (body), IBM Plex Mono (numerals).

### Behaviour
- Single screen, no routing. Library left (60%), now-playing right (40%).
- Folder → track list → tap to play; queues the rest of the folder behind it.
- Queue, shuffle, repeat (off/all/one), seek, volume, mute.
- Crossfade on track change; needle driven by a `requestAnimationFrame` loop
  that runs **only while playing**.
- Download tray ("service panel"), settings modal, toasts.
- Keyboard: Space/K play-pause, ←/→ seek, ↑/↓ volume, N/P next/prev, D tray,
  Esc close/back. Media keys via the Media Session API.
- **Demo mode**: with no backend reachable it shows a `DEMO` chip and falls back
  to a hardcoded library, procedurally generated SVG cover art, a virtual
  transport (playback simulated so the UI is fully explorable), and a simulated
  download pipeline.

### Verified
- Renders correctly at 1920×1080 (exact 60/40 split, 669px hero art, VU meters
  visible) and 1024×600 (compact mode, everything above the fold).
- Live playback against the real backend, including seek and auto-advance.

---

## 2. Backend

```
server.py                Flask app, routes, WebSocket hub, thread startup
engine/
  config.py              settings registry, atomic JSON persistence, paths
  logging_setup.py       rotating file log + crash hooks
  spotify_api.py         URL parsing, metadata preview, short-lived cache
  downloader.py          spotDL driven headlessly behind the callbacks
library/
  db.py                  SQLite schema, WAL, library queries, job queue
  covers.py              embedded art extraction, content-hash cache
  scanner.py             stat-first filesystem walker and indexer
worker/
  download_worker.py     job queue consumer, wires engine → DB → WebSocket
static/index.html        the frontend
systemd/slugbox.service  unit template (install.sh fills in user and paths)
install.sh               Pi setup: apt deps, venv, systemd, optional kiosk
requirements.txt
data/                    runtime: library.db, .cache/covers/, logs/
```

### API
| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/library` | full index |
| GET | `/api/cover/<hash>.jpg` | cached art, 16-hex-char hashes only |
| GET | `/music/<path>` | audio, **Range supported** so seeking works |
| GET | `/api/preview?url=` | metadata only, no job, no FFmpeg needed |
| POST | `/api/download` | `{"url": …}` → `{job_id, name, track_count, cover}` |
| GET | `/api/download/status` | `{active, queued, history[]}` |
| POST | `/api/download/<id>/cancel` | cancels running or queued |
| GET/POST | `/api/settings` | reads/writes the same `config.json` the engine uses |
| POST | `/api/rescan` | force a rescan |
| GET | `/api/health` | version, counts, FFmpeg status, active job |
| WS | `/ws` | event stream |

WebSocket events: `download_queued`, `download_started`, `download_progress`,
`track_progress`, `track_landed`, `track_failed`, `download_complete`,
`download_failed`, `library_updated`. Unknown events are ignored client-side on
purpose, so the server can add more without breaking an older kiosk.

### Design decisions
- **The scanner owns the index.** When a download lands a file the worker does
  not write the track row — it nudges the scanner, which reads real tags off
  disk. One writer, one source of truth; the index reflects what is actually on
  disk rather than what the downloader believed it wrote.
- **SQLite in WAL mode.** Three things touch it concurrently (Flask request
  threads reading, scanner writing, worker writing). In the default rollback
  journal one writer blocks every reader, which shows up as the kiosk stalling
  mid-scan. Connections are thread-local.
- **Polling, not inotify.** The music root is often a USB stick or network
  mount where inotify silently does nothing. Rescan stats every file and only
  re-reads tags when mtime or size changed.
- **Engine behind six callbacks** (`on_track_started`, `on_track_progress`,
  `on_track_done`, `on_track_skipped`, `on_track_failed`, `on_progress`,
  `on_completed`). The worker never imports spotDL; the engine never imports
  Flask. Swapping engines means rewriting one file.
- **One job at a time**; per-track parallelism capped at `threads: 2` on a Pi.

---

## 3. Engine choice — spotDL, not Sunnify

The brief specified forking the Sunnify CLI engine and called it "MIT-style."
**It isn't.** Sunnify 2.4.2 ships a custom licence whose first condition forbids
modifying any portion of the software, directing modification inquiries to the
author. Confirmed in three places in their repo: `LICENSE`,
`pyproject.toml` (`license = "Custom - Educational Use Only"`), and their
`README.md`. Forking, de-Qt-ing and rebranding it is exactly what that
prohibits.

Using [spotDL](https://github.com/spotDL/spotify-downloader) instead — MIT
licensed, permits modification and redistribution, same pipeline (Spotify
metadata → YouTube Music match → yt-dlp → FFmpeg → Mutagen tags), exposes a
Python API. **No Sunnify code is in this repository.**

Implementation note: spotDL refuses to construct a `Downloader` without FFmpeg,
so the metadata/preview path deliberately uses the bare `SpotifyClient` and
needs no FFmpeg. That's why a box without FFmpeg can still browse and preview,
and gets a clear 503 on download rather than a traceback.

---

## 4. Deviation from the brief

The brief listed Flask-SocketIO. This uses **flask-sock** (plain WebSocket).
The documented contract — path `/ws`, bare JSON frames — *is* plain WebSocket,
not Socket.IO, which uses its own framing at `/socket.io`. Plain WebSocket also
means the browser uses its native API and the frontend ships no client library,
which matters when the whole UI is one self-contained file.

---

## 5. Verified working

- **23/23 API tests pass**: library shape, Range requests (`206` + correct
  `Content-Range`), cover cache, settings round-trip, error paths.
- **Path traversal blocked** on `/music` and `/api/cover` — `../`, URL-encoded
  `..%2f`, and `....//` variants all rejected.
- **Scanner**: corrupt file skipped without crashing, non-audio ignored, covers
  deduplicated by content hash, "Various Artists" derived from mixed tags, loose
  files bucketed into `Singles`, untagged files sorted last.
- **Live playback** through the real backend, including seek and auto-advance
  to the next track.
- **Real download, end to end**: a Spotify track → tagged MP3 on disk with
  correct title / artist / album / year / track number / duration, cover art
  embedded and cached, indexed by the scanner, servable with Range.
- **Playlist folder naming**: a 25-track album landed in its own `Scorpion/`
  folder; a single track correctly goes to `Singles/`.
- **Skip path**: re-running a job skips tracks already on disk.
- **Resume**: a job interrupted by restart is re-queued and picks up where it
  left off.
- **Cancellation** works.
- **Live progress**: landed/skipped/failed counters and current track update in
  real time during a download.

Testing used a generated fixture library of tagged WAV files (real RIFF audio,
real ID3 APIC frames) plus live Spotify metadata.

---

## 6. Bugs found and fixed by running it

**Frontend**
1. Grid rows in `.deck` sized to content — library and stage rendered 887px tall
   inside a 546px deck, pushing the transport off-screen.
2. `display:flex` on `.back` / `.lib-actions` / `.preview` beat the UA `[hidden]`
   rule, so the back button and PLAY/SHUFFLE showed on the shelf view.

**Backend**
3. A UTF-8 BOM in `config.json` silently reverted every setting to defaults.
   Now read as `utf-8-sig`, and parse failures log loudly.
4. Tracks inserted before their parent folder row existed → foreign key failure.
5. Untagged tracks sorted to the top of every folder (SQLite sorts NULL lowest)
   instead of the bottom.
6. `/music/` 404'd on Windows — `Path.relative_to` produces backslashes, which
   `send_from_directory`'s `safe_join` rejects.
7. **Per-track callbacks only fired at job end.** A 25-track album showed
   "0 landed" the whole way through and nothing reached the library until the
   job finished — defeating the core "watch tracks appear live" requirement.
8. Metadata was fetched twice per download (once for the preview card, again on
   enqueue) — about 10s of dead time on a large playlist. Now cached 5 minutes.
9. Folder artist required *unanimity*, so one featured credit ("Drake/JAY-Z")
   and one untagged file labelled a Drake album "Various Artists". Now majority.

---

## 7. Not done

- **Never run on actual Pi hardware.** All testing was on Windows. `install.sh`
  and the systemd unit are written but unexecuted.
- **No committed test suite.** Testing used throwaway scripts in a temp dir.
  Worth adding `tests/` if this keeps growing.
- **Nothing is committed to git.** All files are untracked.

### Known rough edges
- Previewing a large playlist takes ~10s showing only "Reading metadata…".
  Could use a spinner and a disabled DOWNLOAD button.
- Google Fonts won't load on an offline Pi — falls back to Arial Narrow / Segoe
  UI / DejaVu Sans Mono. Self-host the three families to fix.
- Corrupt audio files are re-opened on every scan (they never enter the index,
  so they are never "known"). Negligible, but not free.
- Cancellation granularity is one batch (`threads × 2` tracks), not one track —
  spotDL's pool runs an asyncio gather to completion.
- `/api/settings` has no auth. Fine for a LAN appliance; don't expose it.

---

## 8. Environment changes made

- **FFmpeg installed via winget** (`Gyan.FFmpeg` 9.0.1) so the download path
  could be tested. Reversible: `winget uninstall Gyan.FFmpeg`.
- A Python venv and the Sunnify clone live in the session scratchpad, outside
  this repo.
- `config.json` default points `music_dir` at `~/Music`.

---

- [x] Self-hosted all fonts offline in `static/fonts/` + `static/fonts.css`.
- [x] Added Phone Remote via dynamic QR code at `/remote` for keyboardless jukeboxes.
- [x] Documented physical NFC gravity coin chute mechanism.
- [x] Added automated test suite (`tests/`) with 23 passing tests.
- [x] Git tracking clean and committed.

1. Deploy to the Pi, run `./install.sh --kiosk`, confirm it boots into the
   player.
2. Download a full playlist on the Pi; confirm folder naming and live library
   updates.
3. Check playback doesn't stutter while a download runs (this is what
   `threads: 2` is for — tune if needed).

---

## Running it

```bash
python server.py          # then open http://localhost:5000
```

On the Pi:

```bash
./install.sh --kiosk
```

Frontend alone, no backend: open `static/index.html` directly — it runs in demo
mode.
