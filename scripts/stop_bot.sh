#!/usr/bin/env bash
# 実行中の bot プロセスを停止する。
set -euo pipefail

cd "$(dirname "$0")/.."

# bot プロセスを探して終了（python src/main.py / python -m main 両対応）
PIDS=$(pgrep -f "python.*main" || true)

if [ -z "$PIDS" ]; then
  echo "[stop_bot] 実行中の bot は見つかりませんでした。"
  exit 0
fi

echo "[stop_bot] stopping: $PIDS"
# shellcheck disable=SC2086
kill $PIDS
sleep 2

# まだ残っていたら強制終了
REMAIN=$(pgrep -f "python.*main" || true)
if [ -n "$REMAIN" ]; then
  echo "[stop_bot] force killing: $REMAIN"
  # shellcheck disable=SC2086
  kill -9 $REMAIN
fi

echo "[stop_bot] done."
