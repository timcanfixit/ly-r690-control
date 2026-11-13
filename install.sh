#!/usr/bin/env bash
# LY-R690 web control panel - Raspberry Pi installer.
#
#   ./install.sh [--port /dev/ttyUSB0] [--dry-run]
#
# Creates a Python virtualenv, installs the requirements, writes and enables a systemd service that runs the
# web panel as the CURRENT user, and starts it. Needs sudo for the systemd and serial-group steps only.
# --dry-run prints what would be done (including the service file) and changes nothing.
set -euo pipefail

PORT="/dev/ttyUSB0"
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a device path}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$REPO_DIR/pc-app"
RUN_USER="${SUDO_USER:-$(id -un)}"
SERVICE_NAME="ly-r690-web"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

# Prefer the adapter's stable by-id path when exactly one is present - /dev/ttyUSBn can change between boots.
if [ "$PORT" = "/dev/ttyUSB0" ] && [ -d /dev/serial/by-id ]; then
  mapfile -t found < <(ls -1 /dev/serial/by-id 2>/dev/null || true)
  if [ "${#found[@]}" -eq 1 ]; then PORT="/dev/serial/by-id/${found[0]}"; fi
fi

UNIT="[Unit]
Description=LY-R690 web control panel
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${APP_DIR}/src
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/src/web_server.py ${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"

if [ "$DRY" -eq 1 ]; then
  case "$APP_DIR" in *" "*) echo "[dry-run] WARNING: path contains spaces - the real run would refuse" ;; esac
  echo "[dry-run] user=${RUN_USER} app=${APP_DIR} serial port=${PORT}"
  echo "[dry-run] would: python3 -m venv ${APP_DIR}/venv && pip install -r requirements-web.txt"
  echo "[dry-run] would: add ${RUN_USER} to the 'dialout' group (serial access)"
  echo "[dry-run] would write ${UNIT_PATH}:"
  echo "$UNIT"
  exit 0
fi

case "$APP_DIR" in *" "*) echo "the install path contains spaces ($APP_DIR) - systemd cannot run from it; move the checkout to e.g. ~/ly-r690" >&2; exit 1 ;; esac
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
[ -f "$APP_DIR/requirements-web.txt" ] || { echo "run this from a full checkout (pc-app/ not found)" >&2; exit 1; }

echo "==> Python environment"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements-web.txt"

echo "==> Serial access for ${RUN_USER}"
if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx dialout; then
  sudo usermod -aG dialout "$RUN_USER"
  echo "    added ${RUN_USER} to 'dialout' (the service picks this up on start; log out/in for your own shell)"
fi

echo "==> systemd service"
echo "$UNIT" | sudo tee "$UNIT_PATH" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo "==> Waiting for the first start"
TOKEN=""
for _ in $(seq 1 20); do
  sleep 1
  TOKEN="$(sudo journalctl -u "$SERVICE_NAME" --since '-2min' --no-pager 2>/dev/null | grep -o 'access token (shown once here[^:]*: .*' | tail -1 | sed 's/.*: //')"
  [ -n "$TOKEN" ] && break
done

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo
echo "Installed. Service: sudo systemctl status ${SERVICE_NAME}"
echo "Open:  http://${IP:-<this-pi>}:8080"
if [ -n "$TOKEN" ]; then
  echo "Access token: $TOKEN"
  echo "(stored in ${APP_DIR}/auth_config.json - delete that file and restart the service to get a new one)"
else
  echo "Access token: see ${APP_DIR}/auth_config.json (or: sudo journalctl -u ${SERVICE_NAME} | grep 'access token')"
fi
echo "Serial port in use: ${PORT} - change it in ${UNIT_PATH} if wrong, then: sudo systemctl daemon-reload && sudo systemctl restart ${SERVICE_NAME}"
echo "REMINDER: this controls machine heaters. Never expose port 8080 to the internet; use Tailscale or a TLS reverse proxy."
