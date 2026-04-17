#!/usr/bin/env bash
# =============================================================================
# gmo-bot-safe — 更新スクリプト（git pull + restart）
# -----------------------------------------------------------------------------
# 使い方:  bash deploy/update.sh
# =============================================================================

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/gmo-bot-safe}"
SERVICE_NAME="gmo-bot-safe"

cd "$REPO_DIR"

echo "[update] git pull"
git fetch --all --prune
git pull --ff-only

echo "[update] pip install (変更あれば)"
.venv/bin/pip install -r requirements.txt

echo "[update] systemd restart"
sudo systemctl restart "${SERVICE_NAME}"

sleep 2
sudo systemctl status "${SERVICE_NAME}" --no-pager -l | head -n 15
echo "[update] 完了"
