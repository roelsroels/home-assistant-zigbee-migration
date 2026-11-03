#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install -r "${SCRIPT_DIR}/requirements.txt"

if [[ ! -f "${SCRIPT_DIR}/config.toml" ]]; then
  cp "${SCRIPT_DIR}/config.example.toml" "${SCRIPT_DIR}/config.toml"
  echo "Created config.toml from config.example.toml"
fi

echo "Setup complete. Export HA_TOKEN, MQTT_USERNAME and MQTT_PASSWORD before use."
echo "Run: ${VENV_DIR}/bin/python -m ha_zigbee_migrator --help"
