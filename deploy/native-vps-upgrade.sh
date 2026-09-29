#!/usr/bin/env bash
set -Eeuo pipefail

# 本机与 VPS 共用同一升级状态机；远端 worker 由 systemd 独立托管。
exec python3 "$(dirname "$0")/native_vps_upgrade.py" "$@"
