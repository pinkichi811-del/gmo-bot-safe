#!/usr/bin/env python3
"""現 CHAMPION (5min trend>=5 ma=5/20) の TP/SL 小刻みスイープ。

ベース設定:
  5min, BTC 単独, trend=5, ma=5/20, cooldown=0, max_positions=1

TP x SL 9 組合せ:
  TP: [3.0, 3.5, 4.0]
  SL: [-1.5, -2.0, -2.5]

2024-01-01 〜 2026-03-31 を train / val / final で評価。
fee 0.05%/片側。

使用例:
  python scripts/sweep_tp_sl.py
  python scripts/sweep_tp_sl.py --tf 1H   # 1H 版の TP/SL を試したい時
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_v1_tf import (  # noqa: E402
    aggregate, analyze, load_btc_candles, monthly_breakdown, run_bt, PERIODS,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--out", default="./data/backtest/tp_sl_sweep")
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    ap.add_argument("--tf", choices=("5min", "15min", "1H"), default="5min",
                    help="どの時間足で TP/SL スイープを回すか")
    ap.add_argument("--buy-trend", type=float, default=5.0)
    ap.add_argument("--ma-short", type=int, default=5)
    ap.add_argument("--ma-long", type=int, default=20)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[load] candles", flush=True)
    candles_5m = load_btc_candles(Path(args.data))
    print(f"[load] 5m: {len(candles_5m)} candles", flush=True)

    tf_map = {"5min": 5, "15min": 15, "1H": 60}
    tf_min = tf_map[args.tf]
    candles = aggregate(candles_5m, tf_min) if tf_min != 5 else candles_5m
    print(f"[tf] {args.tf}: {len(candles)} candles", flush=True)
    ts_list = [c.ts for c in candles]

    tp_grid = [3.0, 3.5, 4.0]
    sl_grid = [-1.5, -2.0, -2.5]

    all_results: dict[str, dict] = {}
    t_start = time.time()
    for tp in tp_grid:
        for sl in sl_grid:
            label = f"{args.tf} trend>={args.buy_trend} ma={args.ma_short}/{args.ma_long} TP+{tp}/SL{sl}"
            print(f"\n==== {label} ====", flush=True)
            all_results[label] = {
                "override": {"buy_trend": args.buy_trend, "ma_s": args.ma_short,
                             "ma_l": args.ma_long, "tp_pct": tp, "sl_pct": sl,
                             "tf": args.tf},
            }
            for pname in ("train", "val", "final"):
                res = run_bt(
                    candles, ts_list, PERIODS[pname],
                    buy_trend=args.buy_trend, ma_short=args.ma_short,
                    ma_long=args.ma_long, tp_pct=tp, sl_pct=sl,
                    fee_rate=args.fee_rate,
                )
                st = analyze(res, PERIODS[pname])
                mo = monthly_breakdown(res)
                all_results[label][pname] = {"stats": st, "monthly": mo}
                print(f"  [{pname:5s}] trades={st['trades']:4d}  "
                      f"tpm={st['trades_per_month']:5.1f}  "
                      f"net={st['total_pnl_net_pct']:+.3f}%  "
                      f"win={st['win_rate_pct']:.1f}%  "
                      f"PF={st['profit_factor']:.2f}  "
                      f"DD={st['max_drawdown_pct']:.3f}%  "
                      f"streak={st['longest_losing_streak']}")
    print(f"\n[done] elapsed {time.time()-t_start:.1f}s")

    # rank by composite (train PF + val PF + final PF - dd penalty)
    ranked = []
    for label, d in all_results.items():
        t = d["train"]["stats"]
        v = d["val"]["stats"]
        f = d["final"]["stats"]
        score = (
            (t["profit_factor"] if t["profit_factor"] != float("inf") else 2.0) * 0.3
            + (v["profit_factor"] if v["profit_factor"] != float("inf") else 2.0) * 0.35
            + (f["profit_factor"] if f["profit_factor"] != float("inf") else 2.0) * 0.35
            - max(t["max_drawdown_pct"], v["max_drawdown_pct"], f["max_drawdown_pct"]) * 0.5
        )
        ranked.append({
            "label": label, "score": score,
            "train_pf": t["profit_factor"], "val_pf": v["profit_factor"],
            "final_pf": f["profit_factor"],
            "train_net": t["total_pnl_net_pct"], "val_net": v["total_pnl_net_pct"],
            "final_net": f["total_pnl_net_pct"],
            "trades": t["trades"] + v["trades"] + f["trades"],
            "max_dd": max(t["max_drawdown_pct"], v["max_drawdown_pct"], f["max_drawdown_pct"]),
        })
    ranked.sort(key=lambda r: -r["score"])

    print("\n=== ranking (score = 0.3*train_PF + 0.35*val_PF + 0.35*final_PF - 0.5*maxDD) ===")
    print(f"{'rank':>4}  {'label':<55}  {'score':>6}  "
          f"{'train PF/net':>14}  {'val PF/net':>13}  {'final PF/net':>14}")
    for i, r in enumerate(ranked, 1):
        tpf = r["train_pf"] if r["train_pf"] != float("inf") else 99.0
        vpf = r["val_pf"] if r["val_pf"] != float("inf") else 99.0
        fpf = r["final_pf"] if r["final_pf"] != float("inf") else 99.0
        print(
            f"{i:>4}  {r['label']:<55}  {r['score']:>+6.2f}  "
            f"{tpf:4.2f}/{r['train_net']:+6.2f}%  "
            f"{vpf:4.2f}/{r['val_net']:+6.2f}%  "
            f"{fpf:4.2f}/{r['final_net']:+6.2f}%"
        )

    # save
    (out_dir / f"sweep_{args.tf}.json").write_text(
        json.dumps({"results": all_results, "ranking": ranked},
                   ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    with (out_dir / f"sweep_{args.tf}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "label", "period", "trades", "tpm", "net_pnl_pct", "win_rate_pct",
            "profit_factor", "max_dd_pct", "streak", "avg_win", "avg_loss",
        ])
        for label, d in all_results.items():
            for p in ("train", "val", "final"):
                st = d[p]["stats"]
                pf = st["profit_factor"]
                w.writerow([
                    label, p, st["trades"],
                    round(st["trades_per_month"], 2),
                    round(st["total_pnl_net_pct"], 3),
                    round(st["win_rate_pct"], 1),
                    round(pf, 2) if pf != float("inf") else "inf",
                    round(st["max_drawdown_pct"], 3),
                    st["longest_losing_streak"],
                    round(st["avg_win_jpy"], 0),
                    round(st["avg_loss_jpy"], 0),
                ])
    print(f"\n[save] {out_dir}/sweep_{args.tf}.json, sweep_{args.tf}.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
