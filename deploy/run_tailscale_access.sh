#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TOKEN="$(grep '^API_ACCESS_TOKEN=' .env.docker | cut -d'=' -f2- || true)"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale is not installed. Install it with: brew install tailscale"
  exit 1
fi

if ! tailscale status --json >/dev/null 2>&1; then
  echo "Tailscale is not running or not authenticated on this Mac."
  echo "Open the Tailscale app or run: tailscale up"
  exit 1
fi

TAILSCALE_IP="$(tailscale ip -4 | head -n1)"

if [[ -z "$TAILSCALE_IP" ]]; then
  echo "Could not determine a Tailscale IPv4 address."
  exit 1
fi

if command -v colima >/dev/null 2>&1; then
  if ! colima status >/dev/null 2>&1; then
    echo "Starting colima..."
    colima start --cpu 4 --memory 6 --disk 60
  fi
fi

echo "Starting Docker stack..."
docker compose up -d --build

echo "Waiting for backend health..."
for _ in {1..30}; do
  if curl -fsS --max-time 5 "http://127.0.0.1/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo
echo "Abyss Intuition is reachable over Tailscale."
echo "Use this in the iPhone app:"
echo "  URL:   http://$TAILSCALE_IP"
if [[ -n "$TOKEN" ]]; then
  echo "  Token: $TOKEN"
fi
echo
echo "Protected route check:"
curl -fsS --max-time 10 -H "Authorization: Bearer $TOKEN" "http://$TAILSCALE_IP/bot/demo/bybit/config" | head -c 300 || true
echo
