#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <tunnel-name> <hostname>"
  echo "Example: $0 abyssintuition-api api.example.com"
  exit 1
fi

TUNNEL_NAME="$1"
HOSTNAME="$2"
CONFIG_DIR="${HOME}/.cloudflared"
CONFIG_FILE="${CONFIG_DIR}/config.yml"
TEMPLATE_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/config.template.yml"

mkdir -p "${CONFIG_DIR}"

if [[ ! -f "${CONFIG_DIR}/cert.pem" ]]; then
  echo "No Cloudflare origin cert found."
  echo "Run this first and complete browser login:"
  echo "  cloudflared tunnel login"
  exit 1
fi

echo "Creating tunnel ${TUNNEL_NAME}..."
CREATE_OUTPUT="$(cloudflared tunnel create "${TUNNEL_NAME}")"
echo "${CREATE_OUTPUT}"

TUNNEL_ID="$(printf '%s\n' "${CREATE_OUTPUT}" | awk '/Created tunnel/ {print $4}' | tr -d '\r')"
if [[ -z "${TUNNEL_ID}" ]]; then
  echo "Failed to parse tunnel id from output."
  exit 1
fi

sed \
  -e "s/REPLACE_TUNNEL_ID/${TUNNEL_ID}/g" \
  -e "s/REPLACE_HOSTNAME/${HOSTNAME}/g" \
  "${TEMPLATE_FILE}" > "${CONFIG_FILE}"

echo "Routing DNS ${HOSTNAME} -> tunnel ${TUNNEL_ID}..."
cloudflared tunnel route dns "${TUNNEL_ID}" "${HOSTNAME}"

echo
echo "Done."
echo "Tunnel ID: ${TUNNEL_ID}"
echo "Config written to: ${CONFIG_FILE}"
echo
echo "Start it with:"
echo "  cloudflared tunnel run ${TUNNEL_NAME}"
echo
echo "Or run as a background service with:"
echo "  brew services start cloudflared"
