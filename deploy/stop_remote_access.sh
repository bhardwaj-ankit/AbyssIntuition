#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "${ROOT_DIR}"

pkill -f 'cloudflared tunnel --url http://127.0.0.1 --no-autoupdate' >/dev/null 2>&1 || true
docker compose down

echo "Remote tunnel stopped and Docker services shut down."
