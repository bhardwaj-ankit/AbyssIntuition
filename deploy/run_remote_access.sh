#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_TOKEN_FILE="${ROOT_DIR}/.env.docker"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd colima
require_cmd docker
require_cmd cloudflared
require_cmd curl

cd "${ROOT_DIR}"

if [[ ! -f "${API_TOKEN_FILE}" ]]; then
  echo "Missing ${API_TOKEN_FILE}"
  echo "Create it first from .env.docker.example."
  exit 1
fi

if ! colima status >/dev/null 2>&1; then
  echo "Starting colima..."
  colima start --cpu 4 --memory 6 --disk 60
else
  echo "colima is already running."
fi

echo "Starting Docker services..."
docker compose up -d --build

echo "Waiting for local healthcheck..."
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

if ! curl -fsS http://127.0.0.1/health >/dev/null 2>&1; then
  echo "Local API did not become healthy."
  echo "Check: docker compose logs app --tail=100"
  exit 1
fi

API_TOKEN="$(awk -F= '/^API_ACCESS_TOKEN=/{print $2}' "${API_TOKEN_FILE}" | tail -n 1)"

echo
echo "Local API is healthy."
echo "LAN URL: http://172.20.10.2"
if [[ -n "${API_TOKEN}" ]]; then
  echo "API token: ${API_TOKEN}"
fi
echo
echo "Starting free remote tunnel..."
echo "Keep this terminal open while using the remote URL."
echo

exec cloudflared tunnel --url http://127.0.0.1 --no-autoupdate
