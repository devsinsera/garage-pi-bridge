#!/usr/bin/env bash
# Garage Pi Bridge — first-boot installer for DietPi (Pi 5).
#
# Idempotent: re-running is safe. Installs Python deps, sets up the
# systemd service, primes the .env from .env.example. Does NOT pair
# the OBDLink MX+ — that step is interactive via bluetoothctl.

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root (sudo bash install.sh)" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="garage-obd-bridge"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

echo "→ Installing OS packages…"
apt-get update -qq
apt-get install -y -qq \
  python3 python3-pip python3-venv \
  bluetooth bluez bluez-tools \
  libglib2.0-dev

echo "→ Setting up Python venv at ${ROOT}/.venv …"
if [[ ! -d "${ROOT}/.venv" ]]; then
  python3 -m venv "${ROOT}/.venv"
fi
"${ROOT}/.venv/bin/pip" install --quiet --upgrade pip
"${ROOT}/.venv/bin/pip" install --quiet -r "${ROOT}/requirements.txt"

echo "→ Enabling bluetooth daemon…"
systemctl enable bluetooth >/dev/null 2>&1 || true
systemctl restart bluetooth

echo "→ Priming .env from .env.example (if .env missing)…"
if [[ ! -f "${ROOT}/.env" ]]; then
  cp "${ROOT}/.env.example" "${ROOT}/.env"
  chmod 600 "${ROOT}/.env"
  echo "  → ${ROOT}/.env created from example — edit before starting the service."
else
  echo "  → ${ROOT}/.env already present, untouched."
fi

echo "→ Installing systemd unit at ${UNIT_PATH} …"
cat > "${UNIT_PATH}" <<UNIT
[Unit]
Description=Sinsera Garage OBD bridge (OBDLink MX+ → Supabase)
After=bluetooth.service network-online.target
Wants=bluetooth.service network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT}
ExecStart=${ROOT}/.venv/bin/python ${ROOT}/bridge.py
Restart=on-failure
RestartSec=10
# Pi needs sudo to talk to bluetooth raw sockets; running as root is
# acceptable here because this is a dedicated single-purpose device.
User=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}" >/dev/null

echo ""
echo "✓ Install complete."
echo ""
echo "Next steps:"
echo "  1. Pair OBDLink MX+ once via:   sudo bluetoothctl"
echo "  2. Edit ${ROOT}/.env (Supabase creds + CAR_ID + OBDLINK_MAC)"
echo "  3. Start the service:           sudo systemctl restart ${SERVICE_NAME}"
echo "  4. Tail logs:                   journalctl -u ${SERVICE_NAME} -f"
