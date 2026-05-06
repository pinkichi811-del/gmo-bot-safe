#!/usr/bin/env bash
# 新 champion (本命) と旧 champion (並走) を別 STATE_DIR / LOG_DIR で同時起動する。
# Phase 2 観察用。実注文は両方とも出さない（dry-run only）。
# Ctrl+C で両方停止。
set -euo pipefail

cd "$(dirname "$0")/.."

# .env を読み込む（存在すれば）
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

export RUN_MODE=dry_run

# --- 本命: 新 champion (balanced_ndx, BTC+ETH) ------------------------------
PRIMARY_CONFIG="${PRIMARY_CONFIG:-./config/strategies/btc_eth_balanced_ndx.yaml}"
PRIMARY_STATE="${PRIMARY_STATE:-./data}"
PRIMARY_LOG="${PRIMARY_LOG:-./logs}"

# --- 並走: 旧 champion (5min trend>=5 ma=5/20, BTC 単独) --------------------
COMPARE_CONFIG="${COMPARE_CONFIG:-./config/app.yaml}"
COMPARE_STATE="${COMPARE_STATE:-./data/compare_old}"
COMPARE_LOG="${COMPARE_LOG:-./logs/compare_old}"

mkdir -p "$PRIMARY_STATE" "$PRIMARY_LOG" "$COMPARE_STATE" "$COMPARE_LOG"

# PyYAML チェック
if ! python -c "import yaml" >/dev/null 2>&1; then
  echo "[compare] PyYAML が見つかりません。pip install -r requirements.txt を実行してください。"
  exit 1
fi

# Windows Git Bash / Linux 両対応の python コマンド検出 (既存 dry_run.sh と同様)
PYTHON_CMD="${PYTHON_CMD:-python}"
if ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=python3
  fi
fi

echo "[compare] primary:"
echo "[compare]   CONFIG_PATH=$PRIMARY_CONFIG"
echo "[compare]   STATE_DIR=$PRIMARY_STATE"
echo "[compare]   LOG_DIR=$PRIMARY_LOG"
echo "[compare] compare (parallel):"
echo "[compare]   CONFIG_PATH=$COMPARE_CONFIG"
echo "[compare]   STATE_DIR=$COMPARE_STATE"
echo "[compare]   LOG_DIR=$COMPARE_LOG"
echo "[compare] Press Ctrl+C to stop both."

# 両プロセスをバックグラウンド起動。Ctrl+C で trap して両方を kill。
PIDS=()
cleanup() {
  echo
  echo "[compare] stopping both processes..."
  for p in "${PIDS[@]}"; do
    kill "$p" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo "[compare] stopped."
}
trap cleanup INT TERM

CONFIG_PATH="$PRIMARY_CONFIG" STATE_DIR="$PRIMARY_STATE" LOG_DIR="$PRIMARY_LOG" \
  "$PYTHON_CMD" src/main.py >> "$PRIMARY_LOG/console.log" 2>&1 &
PIDS+=("$!")

CONFIG_PATH="$COMPARE_CONFIG" STATE_DIR="$COMPARE_STATE" LOG_DIR="$COMPARE_LOG" \
  "$PYTHON_CMD" src/main.py >> "$COMPARE_LOG/console.log" 2>&1 &
PIDS+=("$!")

echo "[compare] PIDs: ${PIDS[*]}"
wait
