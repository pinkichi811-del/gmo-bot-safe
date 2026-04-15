#!/usr/bin/env bash
# live 起動スクリプト。
#
# ★現段階では封印されています。★
#   - src/order_executor.py の ENABLE_LIVE_ORDER=False（コードゲート）
#   - _send_live_order は NotImplementedError（実装未着手）
#
# このスクリプトは「将来 live を開くとき」の雛形。今は実行しても
# どの注文も送信されません（code gate で全ブロック）。
set -euo pipefail

cd "$(dirname "$0")/.."

cat <<'EOF'
===========================================================
 gmo-bot-safe: live 起動スクリプト
 注意: live 発注は封印されています。
   gate1 ENABLE_LIVE_ORDER (src/order_executor.py) = False
   gate2 LIVE_OK env var                           = must be 'yes'
   gate3 _send_live_order                           = 未実装
 起動しても全注文は "blocked_by_code_gate" ステータスで
 dropped されます。dry_run.sh を使ってください。
===========================================================
EOF

# 二段階の明示承認: CONFIRM_LIVE=yes AND LIVE_OK=yes
if [ "${CONFIRM_LIVE:-no}" != "yes" ]; then
  echo "[run_live] CONFIRM_LIVE=yes が必要です。中止。"
  exit 1
fi
if [ "${LIVE_OK:-no}" != "yes" ]; then
  echo "[run_live] LIVE_OK=yes が必要です。中止。"
  exit 1
fi

# .env を読み込む
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . .env
  set +a
fi

export RUN_MODE=live
export CONFIG_PATH="${CONFIG_PATH:-./config/app.yaml}"
mkdir -p "${STATE_DIR:-./data}" "${LOG_DIR:-./logs}"

echo "[run_live] 起動します。ただし code gate は closed です（=全注文 drop）。"
exec python src/main.py
