#!/usr/bin/env bash
# dry-run モードで bot を起動する。
# - 実注文は絶対に出さない（order_executor は記録のみ）
# - 市場データは StubMarketDataSource（合成データ）
# - Ctrl+C で停止
set -euo pipefail

cd "$(dirname "$0")/.."

# .env を読み込む（存在すれば）
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

# モード固定（.env の RUN_MODE を上書き）
export RUN_MODE=dry_run

# 設定ファイル
export CONFIG_PATH="${CONFIG_PATH:-./config/app.yaml}"

# state / log ディレクトリ
mkdir -p "${STATE_DIR:-./data}" "${LOG_DIR:-./logs}"

# PyYAML の存在確認（無ければヒントを出して終了）
if ! python -c "import yaml" >/dev/null 2>&1; then
  echo "[dry_run] PyYAML が見つかりません。以下を実行してください:"
  echo "  pip install -r requirements.txt"
  exit 1
fi

echo "[dry_run] starting bot"
echo "[dry_run]   RUN_MODE=$RUN_MODE"
echo "[dry_run]   CONFIG_PATH=$CONFIG_PATH"
echo "[dry_run]   STATE_DIR=${STATE_DIR:-./data}"
echo "[dry_run]   LOG_DIR=${LOG_DIR:-./logs}"

# src/main.py を直接実行。Python が src/ を自動で sys.path に入れるので
# PYTHONPATH の OS 差異（Windows ';' vs Unix ':'）の影響を受けない。
exec python src/main.py
