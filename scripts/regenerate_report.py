#!/usr/bin/env python3
"""summary.csv から top20 JSON と REPORT.md を再生成する。

用途: 大量 trial が途中で止まった時に、部分結果 summary.csv を元に解析だけ回す。
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_v1_tf import load_btc_candles  # noqa: E402
from regime_filter import load_daily_csv, generate_events_calendar  # noqa: E402
from optimize_local import rerun_with_trades, format_report  # noqa: E402


def _to_int(v: str, default: int = 0) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _to_float(v: str, default: float = 0.0) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--market", default="./data/market")
    ap.add_argument("--dir", default="./data/backtest/optimize_local")
    ap.add_argument("--workers", type=int, default=6)  # REPORT 用ダミー
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--beta", type=float, default=0.4)
    ap.add_argument("--gamma", type=float, default=0.3)
    ap.add_argument("--trials", type=int, default=0)
    ap.add_argument("--stage1-frac", type=float, default=0.8)
    ap.add_argument("--stage2-k", type=int, default=30)
    ap.add_argument("--stage2-neighbors", type=int, default=15)
    args = ap.parse_args()

    base = Path(args.dir)
    summary = base / "summary.csv"
    top_dir = base / "top20"
    top_dir.mkdir(exist_ok=True)

    print(f"[load] {summary}", flush=True)
    with summary.open(encoding="utf-8", newline="") as f:
        rows_raw = list(csv.DictReader(f))
    print(f"[load] {len(rows_raw)} rows", flush=True)

    # 型変換 + ok のみ
    rows: list[dict] = []
    for r in rows_raw:
        if r.get("status") != "ok":
            continue
        conv = dict(r)
        for k in ("trial_id", "stage", "buy_trend", "ma_short", "ma_long",
                  "max_hold_bars", "cooldown_min",
                  "use_ndx", "ndx_ma_short", "ndx_ma_long",
                  "use_spx", "spx_ma_short", "spx_ma_long",
                  "use_vix", "use_us_hours", "use_events", "events_window_min",
                  "train_extra_trades", "train_trades", "val_trades", "final_trades",
                  "trade_guard"):
            if k in conv:
                conv[k] = _to_int(conv[k])
        for k in ("tp_pct", "sl_pct", "trail_pct", "vix_max",
                  "train_extra_pf", "train_pf", "val_pf", "final_pf",
                  "train_extra_dd", "train_dd", "val_dd", "final_dd",
                  "train_extra_net_pct", "train_net_pct", "val_net_pct", "final_net_pct",
                  "pf_mean", "pf_std", "dd_max", "pf_spread", "composite", "wall_sec"):
            if k in conv:
                conv[k] = _to_float(conv[k])
        rows.append(conv)

    rows.sort(key=lambda r: r["composite"], reverse=True)
    print(f"[rank] {len(rows)} ok rows. top composite: {rows[0]['composite']}",
          flush=True)

    # データロード
    candles = load_btc_candles(Path(args.data))
    ts_list = [c.ts for c in candles]
    ndx_bars = load_daily_csv(Path(args.market) / "NDX_d.csv")
    spx_bars = load_daily_csv(Path(args.market) / "SPX_d.csv")
    vix_bars = load_daily_csv(Path(args.market) / "VIX_d.csv")
    events = generate_events_calendar(2022, 2026)
    print(f"[load] candles={len(candles)} ndx={len(ndx_bars)} spx={len(spx_bars)} "
          f"vix={len(vix_bars)} events={len(events)}", flush=True)

    t0 = time.time()
    print("[top] Re-running top 20 for detailed trades...", flush=True)
    for rank, row in enumerate(rows[:20], 1):
        param = {
            "buy_trend": row["buy_trend"],
            "ma_short": row["ma_short"], "ma_long": row["ma_long"],
            "tp_pct": row["tp_pct"], "sl_pct": row["sl_pct"],
            "max_hold_bars": row.get("max_hold_bars", 0),
            "trail_pct": row.get("trail_pct", 0.0),
            "cooldown_min": row.get("cooldown_min", 0),
            "use_ndx": bool(row["use_ndx"]),
            "ndx_ma_short": row["ndx_ma_short"] or None,
            "ndx_ma_long": row["ndx_ma_long"] or None,
            "use_spx": bool(row.get("use_spx", 0)),
            "spx_ma_short": row.get("spx_ma_short") or None,
            "spx_ma_long": row.get("spx_ma_long") or None,
            "use_vix": bool(row.get("use_vix", 0)),
            "vix_max": row.get("vix_max") or None,
            "use_us_hours": bool(row.get("use_us_hours", 0)),
            "use_events": bool(row.get("use_events", 0)),
            "events_window_min": row.get("events_window_min") or None,
        }
        try:
            detail = rerun_with_trades(candles, ts_list, ndx_bars, spx_bars,
                                       vix_bars, events, param)
            payload = {
                "rank": rank, "trial_id": row["trial_id"],
                "composite": row["composite"], "param": param,
                "summary_row": row, "per_period": detail,
            }
            path = top_dir / f"trial_{rank:02d}_id{row['trial_id']}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")
            print(f"  rank {rank}: trial {row['trial_id']} composite={row['composite']:.3f}",
                  flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  rank {rank} re-run failed: {e}", flush=True)

    wall = time.time() - t0
    args.trials = len(rows_raw)  # 実際の trial 数
    report = format_report(rows, args, wall, len(rows_raw), len(rows),
                           len(rows_raw) - len(rows))
    (base / "REPORT.md").write_text(report, encoding="utf-8")
    print(f"\n[save] {base}/REPORT.md", flush=True)
    print(f"[wall] {wall:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
