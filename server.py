#!/usr/bin/env python3
"""slugbox — web server for the Pi jukebox.

Serves the kiosk frontend, the library API, audio with Range support, and a
WebSocket event stream. Owns two background threads: the library scanner and
the download worker.

Run directly for development:

    python server.py

In production systemd starts this same entrypoint; see systemd/slugbox.service.
"""

from __future__ import annotations

import json
import mimetypes
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Set

from flask import (Flask, Response, abort, jsonify, request,
                   send_file, send_from_directory)
from flask_sock import Sock

from engine import config, spotify_api
from engine.logging_setup import get_logger, setup
from library import covers, db, scanner as scanner_mod
from worker.download_worker import DownloadWorker

log = setup()
api_log = get_logger("server")

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SOCK_SERVER_OPTIONS"] = {"ping_interval": 25}
app.config["JSON_SORT_KEYS"] = False
sock = Sock(app)

# Opus in an .opus container is still missing from some Python installs.
mimetypes.add_type("audio/opus", ".opus")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/mp4", ".m4a")


# --------------------------------------------------------------- websocket ---

class Hub:
    """Fan-out to connected clients.

    Each client owns a bounded queue and its own sending thread. A kiosk browser
    that stops reading can't stall the download worker — its queue fills, we drop
    its backlog, and everyone else carries on.
    """

    def __init__(self) -> None:
        self._clients: Set[queue.Queue] = set()
        self._lock = threading.Lock()

    def register(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.add(client)
        return client

    def unregister(self, client: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(client)

    def broadcast(self, message: Dict[str, Any]) -> None:
        payload = json.dumps(message, default=str)
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(payload)
            except queue.Full:
                # Slow consumer: drop the oldest so live state still gets through.
                try:
                    client.get_nowait()
                    client.put_nowait(payload)
                except (queue.Empty, queue.Full):
                    pass

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


hub = Hub()


@sock.route("/ws")
def ws_stream(ws) -> None:
    client = hub.register()
    api_log.info("WebSocket client connected (%d total)", hub.client_count)
    try:
        ws.send(json.dumps({"event": "hello", "version": config.VERSION}))
        while True:
            try:
                message = client.get(timeout=20.0)
            except queue.Empty:
                # Keepalive; also how we notice a dead peer (send raises).
                message = json.dumps({"event": "ping", "t": int(time.time())})
            ws.send(message)
    except Exception:
        pass
    finally:
        hub.unregister(client)
        api_log.info("WebSocket client gone (%d left)", hub.client_count)


# ------------------------------------------------------------ static + app ---

@app.route("/")
def index() -> Response:
    return send_from_directory(app.static_folder, "index.html")


@app.route("/remote")
def remote() -> Response:
    return send_from_directory(app.static_folder, "remote.html")


@app.route("/favicon.ico")
def favicon() -> Response:
    return Response(status=204)


@app.route("/fonts.css")
def fonts_css() -> Response:
    return send_from_directory(app.static_folder, "fonts.css")


@app.route("/fonts/<path:filename>")
def fonts_static(filename: str) -> Response:
    return send_from_directory(Path(app.static_folder) / "fonts", filename)


# ------------------------------------------------------------------ library ---

@app.route("/api/library")
def api_library() -> Response:
    return jsonify({"folders": db.library_tree()})


@app.route("/api/cover/<path:name>")
def api_cover(name: str) -> Response:
    digest = name.rsplit(".", 1)[0]
    if not covers.is_valid_hash(digest):
        abort(404)
    path = covers.cover_path(digest)
    if not path.exists():
        abort(404)
    return send_file(path, mimetype=covers.cover_mimetype(digest),
                     max_age=31536000, conditional=True)


@app.route("/music/<path:filepath>")
def api_music(filepath: str) -> Response:
    """Serve audio with Range support so the player can seek.

    `send_from_directory` handles Range/If-Range when conditional=True. The
    resolve() check below is the actual security boundary — without it a
    crafted path could read anything the service user can read.
    """
    root = config.music_dir()
    try:
        target = (root / filepath).resolve()
        target.relative_to(root)      # the security boundary: must stay inside
    except (ValueError, OSError):
        abort(403)

    if not target.is_file():
        abort(404)

    # send_file rather than send_from_directory: the path is already resolved
    # and containment-checked above, and send_from_directory's safe_join
    # rejects the backslash separators that Path produces on Windows.
    return send_file(
        target,
        conditional=True,              # this is what enables Range / seeking
        max_age=3600,
        download_name=target.name,
    )


# ---------------------------------------------------------------- downloads ---

@app.route("/api/preview")
def api_preview() -> Response:
    """Metadata only — creates no job. Works without FFmpeg installed."""
    url = (request.args.get("url") or "").strip()
    try:
        preview = spotify_api.preview(url)
    except spotify_api.InvalidURL as exc:
        return jsonify({"error": str(exc)}), 400
    except spotify_api.SpotifyError as exc:
        return jsonify({"error": str(exc)}), 502

    # The confirmation card doesn't need the whole track list; the tray shows
    # per-track rows once the job is running.
    preview["tracks"] = preview["tracks"][:200]
    return jsonify(preview)


@app.route("/api/download", methods=["POST"])
def api_download() -> Response:
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()

    try:
        preview = spotify_api.preview(url)
    except spotify_api.InvalidURL as exc:
        return jsonify({"error": str(exc)}), 400
    except spotify_api.SpotifyError as exc:
        return jsonify({"error": str(exc)}), 502

    if config.ffmpeg_path() is None:
        return jsonify({
            "error": "FFmpeg is not installed, so downloads cannot run. "
                     "Install it with: sudo apt install ffmpeg"
        }), 503

    job_id = worker.enqueue(url, preview)
    return jsonify({
        "job_id": job_id,
        "name": preview["name"],
        "kind": preview["kind"],
        "track_count": preview["track_count"],
        "cover": preview["cover"],
    })


@app.route("/api/download/status")
def api_download_status() -> Response:
    return jsonify(db.job_status())


@app.route("/api/download/<job_id>/cancel", methods=["POST"])
def api_download_cancel(job_id: str) -> Response:
    if worker.cancel(job_id):
        return jsonify({"ok": True, "job_id": job_id})
    return jsonify({"ok": False, "error": "No such active job."}), 404


# ----------------------------------------------------------------- settings ---

@app.route("/api/settings", methods=["GET"])
def api_settings_get() -> Response:
    ffmpeg = config.ffmpeg_path()
    return jsonify({
        "values": config.load(),
        "schema": config.describe(),
        "ffmpeg": {"found": ffmpeg is not None, "path": ffmpeg},
        "version": config.VERSION,
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_post() -> Response:
    body = request.get_json(silent=True) or {}
    updated = config.save(body)
    api_log.info("Settings updated: %s", ", ".join(sorted(body.keys())) or "(none)")
    hub.broadcast({"event": "settings_updated"})
    if "music_dir" in body or "scan_interval_s" in body:
        scanner.nudge()
    return jsonify({"values": updated})


def get_local_ip() -> str:
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


@app.route("/api/health")
def api_health() -> Response:
    port = int(config.get("port") or 5000)
    local_ip = get_local_ip()
    return jsonify({
        "ok": True,
        "version": config.VERSION,
        "library": db.counts(),
        "ffmpeg": config.ffmpeg_path() is not None,
        "ws_clients": hub.client_count,
        "active_job": worker.current_job_id,
        "music_dir": str(config.music_dir()),
        "local_ip": local_ip,
        "remote_url": f"http://{local_ip}:{port}/remote",
    })


@app.route("/api/rescan", methods=["POST"])
def api_rescan() -> Response:
    scanner.nudge()
    return jsonify({"ok": True})


@app.errorhandler(404)
def not_found(_e) -> Response:
    if request.path.startswith(("/api/", "/music/")):
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(app.static_folder, "index.html")


# -------------------------------------------------------------- background ---

def _on_library_change(summary: Dict[str, int]) -> None:
    hub.broadcast({
        "event": "library_updated",
        "added": summary.get("added", 0),
        "removed": summary.get("removed", 0),
        "total": summary.get("total", 0),
    })


scanner = scanner_mod.Scanner(on_change=_on_library_change)
worker = DownloadWorker(broadcast=hub.broadcast, nudge_scanner=scanner.nudge)


def start_background() -> None:
    config.ensure_dirs()
    db.init()
    scanner.start()
    worker.start()
    api_log.info("Background threads started")


def shutdown(*_args: Any) -> None:
    api_log.info("Shutting down")
    scanner.stop()
    worker.stop()
    sys.exit(0)


def main() -> None:
    cfg = config.load()
    ffmpeg = config.ffmpeg_path()

    api_log.info("%s %s starting", config.APP_NAME, config.VERSION)
    api_log.info("Music directory: %s", config.music_dir())
    if ffmpeg:
        api_log.info("FFmpeg: %s", ffmpeg)
    else:
        api_log.warning("FFmpeg NOT found — the library will play fine, but "
                        "downloads will be refused until you install it "
                        "(sudo apt install ffmpeg)")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    start_background()

    host, port = str(cfg["host"]), int(cfg["port"])
    api_log.info("Listening on http://%s:%d", host, port)
    # threaded=True so audio streaming, the WebSocket and API calls don't block
    # one another. Single-user appliance on a LAN; no WSGI server needed.
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
