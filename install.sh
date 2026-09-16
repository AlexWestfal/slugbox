#!/usr/bin/env bash
#
# slugbox installer for Raspberry Pi OS (Debian bookworm or later).
#
#   ./install.sh                 install + enable the service
#   ./install.sh --no-service    set up the venv only, don't touch systemd
#   ./install.sh --kiosk         also install the Chromium kiosk autostart
#
# Safe to re-run: it upgrades dependencies in place and leaves config.json and
# your library database alone.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"
SERVICE_NAME="slugbox"
INSTALL_SERVICE=1
INSTALL_KIOSK=0

for arg in "$@"; do
  case "$arg" in
    --no-service) INSTALL_SERVICE=0 ;;
    --kiosk)      INSTALL_KIOSK=1 ;;
    -h|--help)    sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -eq 0 && -z "${SUDO_USER:-}" ]]; then
  echo "Run this as your normal user (it will sudo where needed), not as root." >&2
  exit 1
fi

say() { printf '\n\033[1;33m==>\033[0m %s\n' "$1"; }

# ----------------------------------------------------------------- packages --
say "Installing system packages"
sudo apt-get update -qq
# ffmpeg is non-negotiable: spotDL refuses to start a download without it.
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip ffmpeg ca-certificates

# --------------------------------------------------------------------- venv --
say "Creating virtualenv at $DIR/.venv"
if [[ ! -d "$DIR/.venv" ]]; then
  python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/python" -m pip install --quiet --upgrade pip wheel
"$DIR/.venv/bin/python" -m pip install --quiet -r "$DIR/requirements.txt"

say "Installed versions"
"$DIR/.venv/bin/python" - <<'PY'
import importlib.metadata as md
for name in ("flask", "flask-sock", "spotdl", "yt-dlp", "mutagen"):
    try:
        print(f"  {name:<12} {md.version(name)}")
    except md.PackageNotFoundError:
        print(f"  {name:<12} MISSING")
PY

# ------------------------------------------------------------------- config --
if [[ ! -f "$DIR/config.json" ]]; then
  say "Writing default config.json"
  MUSIC_DIR="/home/$RUN_USER/Music"
  mkdir -p "$MUSIC_DIR"
  cat > "$DIR/config.json" <<JSON
{
  "music_dir": "$MUSIC_DIR",
  "format": "mp3",
  "bitrate": "320k",
  "threads": 2,
  "host": "0.0.0.0",
  "port": 5000
}
JSON
  echo "    music_dir = $MUSIC_DIR"
else
  say "Keeping existing config.json"
fi

mkdir -p "$DIR/data"

# ------------------------------------------------------------------ service --
if [[ $INSTALL_SERVICE -eq 1 ]]; then
  say "Installing systemd unit"
  UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
  sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$DIR|g" \
      "$DIR/systemd/slugbox.service" | sudo tee "$UNIT" >/dev/null

  sudo systemctl daemon-reload
  sudo systemctl enable "$SERVICE_NAME"
  sudo systemctl restart "$SERVICE_NAME"

  sleep 2
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    PORT="$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port',5000))" 2>/dev/null || echo 5000)"
    say "Running at http://localhost:${PORT}"
    echo "    logs:   journalctl -u ${SERVICE_NAME} -f"
    echo "    stop:   sudo systemctl stop ${SERVICE_NAME}"
  else
    echo "Service failed to start. Recent log:" >&2
    sudo journalctl -u "$SERVICE_NAME" -n 40 --no-pager >&2
    exit 1
  fi
else
  say "Skipped systemd (run: $DIR/.venv/bin/python $DIR/server.py)"
fi

# -------------------------------------------------------------------- kiosk --
if [[ $INSTALL_KIOSK -eq 1 ]]; then
  say "Installing Chromium kiosk autostart"
  PORT="$(python3 -c "import json;print(json.load(open('$DIR/config.json')).get('port',5000))" 2>/dev/null || echo 5000)"
  BROWSER="$(command -v chromium-browser || command -v chromium || true)"
  if [[ -z "$BROWSER" ]]; then
    echo "    chromium not found; install it with: sudo apt install -y chromium-browser" >&2
  else
    AUTOSTART="/home/$RUN_USER/.config/autostart"
    mkdir -p "$AUTOSTART"
    cat > "$AUTOSTART/slugbox-kiosk.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=slugbox kiosk
# --autoplay-policy is what lets the first track start without a tap.
Exec=$BROWSER --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --autoplay-policy=no-user-gesture-required --check-for-update-interval=31536000 http://localhost:$PORT/
X-GNOME-Autostart-enabled=true
DESKTOP
    echo "    $AUTOSTART/slugbox-kiosk.desktop"
  fi
fi

say "Done"
