#!/usr/bin/env bash
# Sync this integration to a Home Assistant instance for hardware testing.
set -euo pipefail

readonly TARGET="root@172.22.0.10:/config/custom_components/tuya_ble/"

cd "$(dirname "$0")"
ssh root@172.22.0.10 "mkdir -p /config/custom_components/tuya_ble"
rsync -aiv --delete \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    custom_components/tuya_ble/ "$TARGET"

echo "Synced Tuya BLE integration to ${TARGET}"
