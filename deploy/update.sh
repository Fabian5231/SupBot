#!/usr/bin/env bash
# Holt die neueste Version aus GitHub und installiert sie neu.
# Aufruf auf dem VPS:  sudo bash /root/SupBot/deploy/update.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SRC_DIR"

echo "==> git pull in $SRC_DIR"
git pull --ff-only

exec bash "$SRC_DIR/deploy/install.sh"
