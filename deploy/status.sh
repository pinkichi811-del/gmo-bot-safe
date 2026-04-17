#!/usr/bin/env bash
# =============================================================================
# gmo-bot-safe — 稼働状況まとめ
# -----------------------------------------------------------------------------
# 使い方:  bash deploy/status.sh
# =============================================================================

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/gmo-bot-safe}"
SERVICE_NAME="gmo-bot-safe"

cd "$REPO_DIR"

echo "=== systemd ==="
sudo systemctl status "${SERVICE_NAME}" --no-pager -l | head -n 12 || true

echo
echo "=== STOP ファイル ==="
if [ -f ./STOP ]; then
    echo "⚠️  STOP ファイル存在 — 新規買いブロック中"
else
    echo "なし（通常稼働）"
fi

echo
echo "=== 直近ログ (20行) ==="
journalctl -u "${SERVICE_NAME}" -n 20 --no-pager || true

echo
echo "=== 今日の集計 ==="
.venv/bin/python scripts/aggregate.py 2>/dev/null || echo "(集計失敗 or データなし)"

echo
echo "=== state.json 要約 ==="
if [ -f data/state.json ]; then
    .venv/bin/python -c "
import json, sys
s = json.load(open('data/state.json'))
print('HALT:', s.get('halt', False))
print('positions:', list(s.get('positions', {}).keys()) or 'なし')
print('cooldowns:', list(s.get('cooldowns', {}).keys()) or 'なし')
"
else
    echo "state.json 未生成"
fi
