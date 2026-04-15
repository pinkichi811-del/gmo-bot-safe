#!/usr/bin/env python3
"""外部レジームフィルター比較バックテスト。

CHAMPION (5min trend>=5 ma=5/20 TP+3.5/SL-2.0) を固定し、
エントリー判定の直後に「地合いフィルター」を噛ませて合否を見る。

フィルター:
  - us_hours: 米株通常市場時間帯 (14:30-21:00 UTC) のみ許可
  - spx_trend: S&P500 が MA5>MA20 の時のみ許可
  - spx_momentum: S&P500 直近 3 日モメンタムが正の時のみ許可
  - ndx_trend: NASDAQ100 が MA5>MA20 の時のみ許可
  - events_30: 主要指標 ±30 分は拒否
  - events_60: 主要指標 ±60 分は拒否
  - vix_20: VIX <= 20 の時のみ許可
  - vix_25: VIX <= 25 の時のみ許可

比較パターン:
  1. baseline (no filter)
  2. us_hours
  3. spx_trend
  4. events_30
  5. us_hours + spx_trend
  6. us_hours + events_30
  7. spx_trend + events_30
  8. us_hours + spx_trend + events_30
  9. + vix_25
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
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_v1_tf import (  # noqa: E402
    analyze, load_btc_candles, monthly_breakdown, PERIODS, precompute_extras,
)
from market_watcher import Candle  # noqa: E402
from regime_filter import (  # noqa: E402
    filter_us_regular_only, load_daily_csv,
    make_event_avoidance_filter, make_index_momentum_filter,
    make_index_trend_filter, make_vix_filter,
    generate_events_calendar,
)


# ---------------------------------------------------------------------------
# フィルター付き backtest
# ---------------------------------------------------------------------------
def run_bt_with_filters(
    candles: list[Candle], ts_list: list[float], period: tuple[float, float],
    buy_trend: float, ma_short: int, ma_long: int,
    tp_pct: float, sl_pct: float,
    filters: list[tuple[str, Callable[[float], bool]]],
    per_trade_jpy: float = 10_000.0, initial_cash: float = 1_000_000.0,
    fee_rate: float = 0.0005,
) -> dict[str, Any]:
    extras = precompute_extras(candles, ma_short, ma_long)
    trend = extras["trend"]
    heat = extras["heat"]
    closes = [c.close for c in candles]

    start, end = period
    i_start = max(ma_long + 5, bisect.bisect_left(ts_list, start))
    i_end = bisect.bisect_right(ts_list, end)

    trades: list[dict[str, Any]] = []
    equity_curve: list[tuple[float, float]] = []
    pos: dict[str, Any] | None = None
    realized_net = 0.0
    filter_skips: Counter = Counter()
    raw_signals = 0  # filter 無視の entry 候補数

    for i in range(i_start, i_end):
        cur_close = closes[i]
        cur_ts = candles[i].ts

        if pos is None:
            if trend[i] >= buy_trend and heat[i] >= -8.0 and (60.0 + trend[i] + heat[i] + 5.0) >= 70.0:
                raw_signals += 1
                passed = True
                for name, fn in filters:
                    if not fn(cur_ts):
                        filter_skips[name] += 1
                        passed = False
                        break
                if passed:
                    pos = {"entry_price": cur_close, "entry_ts": cur_ts, "bars_held": 0}
        else:
            pos["bars_held"] += 1
            pct = (cur_close - pos["entry_price"]) / pos["entry_price"] * 100.0
            reason = None
            if pct <= sl_pct:
                reason = "stop_loss"
            elif pct >= tp_pct:
                reason = "take_profit"
            if reason is not None:
                gross = pct / 100.0 * per_trade_jpy
                fee = per_trade_jpy * fee_rate + (per_trade_jpy + gross) * fee_rate
                net = gross - fee
                trades.append({
                    "entry_ts": pos["entry_ts"], "exit_ts": cur_ts,
                    "entry_price": pos["entry_price"], "exit_price": cur_close,
                    "bars_held": pos["bars_held"],
                    "exit_reason": reason,
                    "gross_pnl_jpy": gross, "fee_jpy": fee, "net_pnl_jpy": net,
                })
                realized_net += net
                pos = None

        if i % 12 == 0:
            unr = 0.0
            if pos is not None:
                unr = (cur_close - pos["entry_price"]) / pos["entry_price"] * per_trade_jpy
            equity_curve.append((cur_ts, initial_cash + realized_net + unr))

    final_unr = 0.0
    if pos is not None:
        final_close = closes[i_end - 1]
        final_unr = (final_close - pos["entry_price"]) / pos["entry_price"] * per_trade_jpy

    return {
        "trades": trades, "equity_curve": equity_curve,
        "final_unrealized_jpy": final_unr,
        "initial_cash": initial_cash, "period": period,
        "raw_signals": raw_signals,
        "filter_skips": dict(filter_skips),
    }


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--market", default="./data/market")
    ap.add_argument("--out", default="./data/backtest/regime")
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    market_dir = Path(args.market)

    print("[load] BTC 5min", flush=True)
    candles = load_btc_candles(Path(args.data))
    ts_list = [c.ts for c in candles]
    print(f"[load] {len(candles)} bars", flush=True)

    # 指数データ
    spx = load_daily_csv(market_dir / "SPX_d.csv")
    ndx = load_daily_csv(market_dir / "NDX_d.csv")
    vix = load_daily_csv(market_dir / "VIX_d.csv")
    print(f"[market] SPX={len(spx)} NDX={len(ndx)} VIX={len(vix)}", flush=True)

    # 経済指標カレンダー
    events = generate_events_calendar(2024, 2026)
    print(f"[events] {len(events)} scheduled events (FOMC/NFP/CPI/PPI)", flush=True)

    # 個別フィルター定義
    f_us = filter_us_regular_only
    f_spx_trend = make_index_trend_filter(spx, ma_short=5, ma_long=20)
    f_spx_mom = make_index_momentum_filter(spx, lookback=3)
    f_ndx_trend = make_index_trend_filter(ndx, ma_short=5, ma_long=20)
    f_events_30 = make_event_avoidance_filter(events, 30, 30)
    f_events_60 = make_event_avoidance_filter(events, 60, 60)
    f_vix_25 = make_vix_filter(vix, max_vix=25.0)
    f_vix_20 = make_vix_filter(vix, max_vix=20.0)

    # 比較パターン
    patterns: list[tuple[str, list[tuple[str, Callable[[float], bool]]]]] = [
        ("baseline (no filter)", []),
        ("us_hours", [("us_hours", f_us)]),
        ("spx_trend", [("spx_trend", f_spx_trend)]),
        ("spx_momentum", [("spx_mom", f_spx_mom)]),
        ("ndx_trend", [("ndx_trend", f_ndx_trend)]),
        ("events_30min_avoid", [("events30", f_events_30)]),
        ("events_60min_avoid", [("events60", f_events_60)]),
        ("vix<=25", [("vix25", f_vix_25)]),
        ("vix<=20", [("vix20", f_vix_20)]),
        ("us_hours + spx_trend",
         [("us_hours", f_us), ("spx_trend", f_spx_trend)]),
        ("us_hours + events_30",
         [("us_hours", f_us), ("events30", f_events_30)]),
        ("spx_trend + events_30",
         [("spx_trend", f_spx_trend), ("events30", f_events_30)]),
        ("us_hours + spx_trend + events_30",
         [("us_hours", f_us), ("spx_trend", f_spx_trend), ("events30", f_events_30)]),
        ("us_hours + spx_trend + events_30 + vix_25",
         [("us_hours", f_us), ("spx_trend", f_spx_trend),
          ("events30", f_events_30), ("vix25", f_vix_25)]),
    ]

    all_results: dict[str, dict] = {}
    for name, flts in patterns:
        print(f"\n==== {name} ====", flush=True)
        all_results[name] = {"filters": [n for n, _ in flts]}
        for pname in ("train", "val", "final"):
            t0 = time.time()
            res = run_bt_with_filters(
                candles, ts_list, PERIODS[pname],
                buy_trend=5, ma_short=5, ma_long=20,
                tp_pct=3.5, sl_pct=-2.0,
                filters=flts, fee_rate=args.fee_rate,
            )
            st = analyze(res, PERIODS[pname])
            mo = monthly_breakdown(res)
            all_results[name][pname] = {
                "stats": st, "monthly": mo,
                "raw_signals": res["raw_signals"],
                "filter_skips": res["filter_skips"],
                "filter_pass_rate": (
                    (res["raw_signals"] - sum(res["filter_skips"].values())) / res["raw_signals"] * 100
                    if res["raw_signals"] > 0 else 0.0
                ),
            }
            pass_rate = all_results[name][pname]["filter_pass_rate"]
            print(f"  ({time.time()-t0:.1f}s)  [{pname:5s}]  "
                  f"raw={res['raw_signals']:4d} → trades={st['trades']:4d} "
                  f"(pass={pass_rate:5.1f}%)  "
                  f"net={st['total_pnl_net_pct']:+.3f}%  "
                  f"win={st['win_rate_pct']:.1f}%  "
                  f"PF={st['profit_factor']:.2f}  "
                  f"DD={st['max_drawdown_pct']:.3f}%  "
                  f"streak={st['longest_losing_streak']}")
            if res["filter_skips"]:
                print(f"    skips: {res['filter_skips']}")

    # save
    (out_dir / "results.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    with (out_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "pattern", "period", "raw_signals", "trades", "pass_rate_pct",
            "tpm", "net_pnl_pct", "win_rate_pct", "profit_factor",
            "max_dd_pct", "streak", "avg_win", "avg_loss",
        ])
        for name, d in all_results.items():
            for p in ("train", "val", "final"):
                st = d[p]["stats"]
                pf = st["profit_factor"]
                w.writerow([
                    name, p, d[p]["raw_signals"], st["trades"],
                    round(d[p]["filter_pass_rate"], 1),
                    round(st["trades_per_month"], 2),
                    round(st["total_pnl_net_pct"], 3),
                    round(st["win_rate_pct"], 1),
                    round(pf, 2) if pf != float("inf") else "inf",
                    round(st["max_drawdown_pct"], 3),
                    st["longest_losing_streak"],
                    round(st["avg_win_jpy"], 0),
                    round(st["avg_loss_jpy"], 0),
                ])
    print(f"\n[save] {out_dir}/results.json, comparison.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
