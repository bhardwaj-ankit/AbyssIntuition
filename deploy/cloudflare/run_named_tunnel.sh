#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <tunnel-name>"
  echo "Example: $0 abyssintuition-api"
  exit 1
fi

cloudflared tunnel run "$1"
