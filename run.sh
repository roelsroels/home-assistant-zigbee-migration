#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${SCRIPT_DIR}/secrets.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/secrets.env"
  set +a
fi

exec "${SCRIPT_DIR}/.venv/bin/python" -m ha_zigbee_migrator --config "${SCRIPT_DIR}/config.toml" "$@"
