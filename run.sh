#!/usr/bin/env bash
#
# slugbox quick run launcher
#
# Starts the slugbox server (if not already running) and launches the
# fullscreen kiosk UI directly onto your screen.
#
# Usage:
#   ./run.sh               # Start server + launch fullscreen kiosk on screen
#   ./run.sh --window      # Launch browser in a standard window (not kiosk)
#   ./run.sh --server-only # Start backend server only (no browser)
#   ./run.sh --stop        # Stop both the kiosk browser and server
#

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# ----------------------------------------------------------------- handle stop --
if [[ "${1:-}" == "--stop" || "${1:-}" == "stop" ]]; then
  echo -e "\033[1;33m==>\033[0m Stopping slugbox..."
  pkill -f "chromium.*5000" 2>/dev/null || true
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet slugbox 2>/dev/null; then
    sudo systemctl stop slugbox 2>/dev/null || true
  fi
  pkill -f "$DIR/.venv/bin/python $DIR/server.py" 2>/dev/null || true
  echo -e "\033[1;32m==>\033[0m Stopped."
  exit 0
fi

WINDOW_MODE=0
SERVER_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --window)      WINDOW_MODE=1 ;;
    --server-only) SERVER_ONLY=1 ;;
    -h|--help)
      sed -n '3,13p' "$0" | sed 's/^#//'
      exit 0
      ;;
  esac
done

say() { printf '\n\033[1;33m==>\033[0m %s\n' "$1"; }
ok()  { printf '\033[1;32m==>\033[0m %s\n' "$1"; }

# ------------------------------------------------------------------ check venv --
if [[ ! -f "$DIR/.venv/bin/python" ]]; then
  echo -e "\033[1;31mError:\033[0m Python virtualenv not found at $DIR/.venv" >&2
  echo "Please run ./install.sh first." >&2
  exit 1
fi

mkdir -p "$DIR/data"

# ----------------------------------------------------------------- read config --
# Auto-generates config.json with defaults if missing/corrupt, then reads port
PORT="$("$DIR/.venv/bin/python" -c "from engine import config; print(config.get('port'))" 2>/dev/null || echo 5000)"


check_server() {
  curl -s -m 1 "http://localhost:${PORT}/api/health" 2>/dev/null | grep -q '"ok":\s*true'
}

# ---------------------------------------------------------------- start server --
if check_server; then
  ok "Slugbox server is already running on port ${PORT}"
else
  say "Starting Slugbox server..."
  # Try systemd if installed and available
  STARTED_VIA_SYSTEMD=0
  if command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files --type=service 2>/dev/null | grep -q "slugbox.service"; then
      sudo systemctl start slugbox 2>/dev/null || true
      STARTED_VIA_SYSTEMD=1
    fi
  fi

  # If not managed by systemd or systemd failed, run directly in background
  if ! check_server; then
    nohup "$DIR/.venv/bin/python" "$DIR/server.py" > "$DIR/data/server.log" 2>&1 &
  fi

  # Wait for server to become responsive
  SERVER_UP=0
  for i in {1..20}; do
    if check_server; then
      SERVER_UP=1
      break
    fi
    sleep 0.5
  done

  if [[ $SERVER_UP -eq 1 ]]; then
    ok "Server started successfully on port ${PORT}"
  else
    echo -e "\033[1;31mError:\033[0m Server failed to start. Last log lines:" >&2
    if [[ $STARTED_VIA_SYSTEMD -eq 1 ]]; then
      sudo journalctl -u slugbox -n 20 --no-pager >&2 || true
    elif [[ -f "$DIR/data/server.log" ]]; then
      tail -n 20 "$DIR/data/server.log" >&2
    fi
    exit 1
  fi
fi

# ------------------------------------------------------------- display info --
LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")"
echo ""
echo "--------------------------------------------------------"
echo "  Slugbox is Live!"
echo "  - Local Jukebox:  http://localhost:${PORT}"
echo "  - Network Access: http://${LOCAL_IP}:${PORT}"
echo "  - Mobile Remote:  http://${LOCAL_IP}:${PORT}/remote"
echo "--------------------------------------------------------"
echo ""

if [[ $SERVER_ONLY -eq 1 ]]; then
  exit 0
fi

# --------------------------------------------------------------- launch screen --
say "Opening display on screen..."

# Ensure display target is set (critical if run via SSH or tty)
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  if [[ -e /tmp/.X11-unix/X0 ]]; then
    export DISPLAY=:0
  elif ls /run/user/*/wayland-0 >/dev/null 2>&1; then
    export WAYLAND_DISPLAY=wayland-0
  else
    export DISPLAY=:0
  fi
fi

BROWSER="$(command -v chromium-browser || command -v chromium || command -v x-www-browser || true)"

if [[ -z "$BROWSER" ]]; then
  echo -e "\033[1;33mWarning:\033[0m Chromium browser not found." >&2
  echo "Install it with: sudo apt-get install -y chromium-browser" >&2
  echo "Or navigate to http://localhost:${PORT} in your installed browser." >&2
  exit 0
fi

# Close any existing slugbox kiosk tabs before launching a fresh one
pkill -f "chromium.*5000" 2>/dev/null || true
sleep 0.5

KIOSK_FLAGS=()
if [[ $WINDOW_MODE -eq 0 ]]; then
  KIOSK_FLAGS=(
    "--kiosk"
    "--noerrdialogs"
    "--disable-infobars"
    "--disable-session-crashed-bubble"
    "--autoplay-policy=no-user-gesture-required"
    "--check-for-update-interval=31536000"
  )
fi

"$BROWSER" "${KIOSK_FLAGS[@]}" "http://localhost:${PORT}/" >/dev/null 2>&1 &

ok "Kiosk opened on screen. (To stop: ./run.sh --stop)"
