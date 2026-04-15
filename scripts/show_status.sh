#!/usr/bin/env bash
# 現在の状態を一目で確認する。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== gmo-bot-safe status ==="

# STOP
if [ -f STOP ]; then
  echo "[STOP] present → buys suppressed"
else
  echo "[STOP] not present"
fi

# live gates
if grep -q 'ENABLE_LIVE_ORDER: bool = True' src/order_executor.py 2>/dev/null; then
  echo "[WARN] gate1 ENABLE_LIVE_ORDER=True (コードゲート開放中)"
else
  echo "gate1 ENABLE_LIVE_ORDER=False (closed)"
fi
echo "gate2 LIVE_OK=${LIVE_OK:-no}"
echo "gate3 _send_live_order: NotImplementedError (実装未着手)"

# state.json
if [ -f data/state.json ]; then
  python - <<'PY'
import json, time
with open("data/state.json", encoding="utf-8") as f:
    s = json.load(f)
print(f"halted: {s.get('halted')}  reason: {s.get('halt_reason') or '-'}")
print(f"error_count: {s.get('error_count', 0)}")
pos = s.get("positions") or {}
print(f"positions ({len(pos)}):")
for sym, p in pos.items():
    print(f"  {sym}  entry={p['entry_price']:.2f}  size_jpy={p['size_jpy']:.0f}")
cd = s.get("cooldown_until") or {}
now = time.time()
active = [(sym, (t - now) / 60.0) for sym, t in cd.items() if t > now]
if active:
    print("cooldowns:")
    for sym, mins in active:
        print(f"  {sym}  {mins:.0f}min left")
PY
else
  echo "state.json not found (bot not yet run)"
fi

# dry-run orders
if [ -f data/dry_run_orders.csv ]; then
  n=$(tail -n +2 data/dry_run_orders.csv | wc -l | tr -d ' ')
  echo "dry-run orders recorded: ${n}"
fi

# 今日のサイクル数
today=$(python -c "from datetime import datetime, timezone; print(datetime.now(timezone.utc).date().isoformat())")
log_file="data/score_log/${today}.jsonl"
if [ -f "${log_file}" ]; then
  n=$(wc -l < "${log_file}" | tr -d ' ')
  echo "today's cycles (${today}): ${n}"
else
  echo "today's score log not found"
fi
