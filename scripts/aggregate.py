#!/usr/bin/env python3
"""観察ログ (data/score_log/*.jsonl) を集計する。

使用例:
  python scripts/aggregate.py                       # 今日（UTC）
  python scripts/aggregate.py --date 2026-04-13     # 特定日
  python scripts/aggregate.py --days 7              # 直近7日

stdlib のみ。追加依存なし。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


def iter_records(paths: Iterable[Path]) -> Iterable[dict]:
    for p in paths:
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (UTC). 省略時は今日")
    ap.add_argument("--days", type=int, default=1, help="終了日から何日分さかのぼるか")
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIR", "./data"))
    ap.add_argument("--top", type=int, default=20, help="verdict 分布で表示する上位数")
    args = ap.parse_args()

    base = Path(args.state_dir) / "score_log"
    if not base.exists():
        print(f"[warn] {base} が存在しません。dry-run を一度回してください。", file=sys.stderr)
        return 1

    end: date
    if args.date:
        end = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(args.days, 1) - 1)

    paths: list[Path] = []
    d = start
    while d <= end:
        paths.append(base / f"{d.isoformat()}.jsonl")
        d += timedelta(days=1)

    total = 0
    halted = 0
    stop_cycles = 0
    errors: Counter = Counter()
    verdicts: Counter = Counter()
    buy_cand: Counter = Counter()
    strong_cand: Counter = Counter()
    decisions: dict[str, dict[str, int]] = defaultdict(lambda: {"buy": 0, "sell": 0})
    sums: dict[str, dict[str, float]] = defaultdict(lambda: {
        "total": 0.0, "trend": 0.0, "liquidity": 0.0, "heat": 0.0,
        "volatility": 0.0, "dup_penalty": 0.0, "cash_bonus": 0.0, "n": 0,
    })

    for rec in iter_records(paths):
        total += 1
        if rec.get("halted"):
            halted += 1
        if rec.get("stop_file"):
            stop_cycles += 1
        for e in rec.get("errors", []) or []:
            errors[str(e)[:80]] += 1
        for ev in rec.get("evaluations", []) or []:
            verdicts[ev.get("verdict", "?")] += 1
            sym = ev.get("symbol", "?")
            if ev.get("buy_candidate"):
                buy_cand[sym] += 1
            if ev.get("strong_buy"):
                strong_cand[sym] += 1
            agg = sums[sym]
            agg["n"] = int(agg["n"]) + 1
            for k in ("total", "trend", "liquidity", "heat",
                      "volatility", "dup_penalty", "cash_bonus"):
                agg[k] += float(ev.get(k, 0.0) or 0.0)
        for dd in rec.get("decisions", []) or []:
            decisions[dd.get("symbol", "?")][dd.get("side", "?")] += 1

    period = (
        f"{start.isoformat()}" if start == end
        else f"{start.isoformat()} … {end.isoformat()}"
    )
    print(f"=== aggregate: {period} (UTC) ===")
    print(f"cycles={total}  halted={halted}  stop_file={stop_cycles}")

    if errors:
        print("\n-- errors --")
        for k, v in errors.most_common(10):
            print(f"  {v:4d}  {k}")

    print("\n-- verdict distribution --")
    for k, v in verdicts.most_common(args.top):
        print(f"  {v:4d}  {k}")

    if sums:
        print("\n-- average scores by symbol --")
        hdr = f"  {'symbol':<10} {'total':>6} {'trend':>6} {'liq':>6} {'heat':>6} {'vol':>6} {'dup':>6} {'cash':>6}   n"
        print(hdr)
        for sym, a in sorted(sums.items()):
            n = int(a["n"]) or 1
            print(f"  {sym:<10} "
                  f"{a['total']/n:6.1f} {a['trend']/n:6.1f} "
                  f"{a['liquidity']/n:6.1f} {a['heat']/n:6.1f} "
                  f"{a['volatility']/n:6.1f} {a['dup_penalty']/n:6.1f} "
                  f"{a['cash_bonus']/n:6.1f}   {int(a['n'])}")

    if buy_cand:
        print("\n-- buy_candidate counts --")
        for sym, c in buy_cand.most_common():
            print(f"  {c:4d}  {sym} (strong={strong_cand.get(sym, 0)})")

    if decisions:
        print("\n-- decisions by symbol --")
        for sym, d in sorted(decisions.items()):
            print(f"  {sym:<10} buy={d['buy']} sell={d['sell']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
