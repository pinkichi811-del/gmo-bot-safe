#!/usr/bin/env python3
"""Multi-symbol backtest with optional regime filters.

各銘柄独立に CHAMPION エントリー (trend>=5 ma=5/20) を判定し、
max_positions の範囲内で同時保有を許す。

使用例:
  python scripts/backtest_multi.py --symbols BTC_JPY
  python scripts/backtest_multi.py --symbols BTC_JPY ETH_JPY XRP_JPY --max-positions 3
  python scripts/backtest_multi.py --symbols BTC_JPY ETH_JPY --filter ndx_trend
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

from market_watcher import Candle  # noqa: E402
from backtest_v1_tf import precompute_extras  # noqa: E402
from backtest_v3 import PERIODS, ts as iso_ts  # noqa: E402
from regime_filter import (  # noqa: E402
    filter_us_regular_only, generate_events_calendar,
    load_daily_csv, make_event_avoidance_filter,
    make_index_trend_filter, make_index_momentum_filter,
    make_vix_filter,
)


# ---------------------------------------------------------------------------
# データロード（各銘柄）
# ---------------------------------------------------------------------------
def load_symbol_candles(data_root: Path, symbol: str) -> list[Candle]:
    all_c: list[Candle] = []
    for fp in sorted(data_root.glob(f"{symbol}_*.json")):
        payload = json.loads(fp.read_text(encoding="utf-8"))
        for c in payload["candles"]:
            all_c.append(Candle(
                ts=int(c["openTime"]) / 1000.0,
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c["volume"]),
            ))
    all_c.sort(key=lambda x: x.ts)
    return all_c


def prepare_symbol(data_root: Path, symbol: str, ma_s: int, ma_l: int) -> dict:
    candles = load_symbol_candles(data_root, symbol)
    if not candles:
        return {}
    ts_list = [c.ts for c in candles]
    ts_idx = {t: i for i, t in enumerate(ts_list)}
    extras = precompute_extras(candles, ma_s, ma_l)
    return {
        "candles": candles, "ts_list": ts_list,
        "ts_idx": ts_idx, "extras": extras,
    }


# ---------------------------------------------------------------------------
# Multi-symbol backtest
# ---------------------------------------------------------------------------
def run_multi(
    symbols_data: dict[str, dict], period: tuple[float, float],
    buy_trend: float, ma_s: int, ma_l: int,
    tp_pct: float, sl_pct: float,
    max_positions: int, per_trade_jpy: float,
    initial_cash: float = 1_000_000.0, fee_rate: float = 0.0005,
    filters: list[tuple[str, Callable[[float], bool]]] | None = None,
) -> dict[str, Any]:
    if filters is None:
        filters = []

    # union of timestamps in period
    all_ts: set[float] = set()
    for sym, d in symbols_data.items():
        for t in d["ts_list"]:
            if period[0] <= t <= period[1]:
                all_ts.add(t)
    sorted_ts = sorted(all_ts)

    positions: dict[str, dict] = {}
    trades: list[dict] = []
    realized = 0.0
    equity_curve: list[tuple[float, float]] = []
    raw_signals = 0
    filter_skips: Counter = Counter()
    portfolio_full_skips = 0

    sample_every = 144  # 1 sample per 144 ts ≈ 12h
    for k, ts in enumerate(sorted_ts):
        # exit pass first
        for sym in list(positions.keys()):
            d = symbols_data[sym]
            i = d["ts_idx"].get(ts)
            if i is None:
                continue
            pos = positions[sym]
            pos["bars_held"] += 1
            cur_close = d["candles"][i].close
            pct = (cur_close - pos["entry_price"]) / pos["entry_price"] * 100.0
            reason = None
            if pct <= sl_pct:
                reason = "stop_loss"
            elif pct >= tp_pct:
                reason = "take_profit"
            if reason is not None:
                gross = pct / 100.0 * pos["size_jpy"]
                fee = pos["size_jpy"] * fee_rate + (pos["size_jpy"] + gross) * fee_rate
                net = gross - fee
                trades.append({
                    "symbol": sym,
                    "entry_ts": pos["entry_ts"], "exit_ts": ts,
                    "entry_price": pos["entry_price"], "exit_price": cur_close,
                    "bars_held": pos["bars_held"],
                    "exit_reason": reason,
                    "size_jpy": pos["size_jpy"],
                    "gross_pnl_jpy": gross, "fee_jpy": fee, "net_pnl_jpy": net,
                })
                realized += net
                del positions[sym]

        # entry pass
        for sym, d in symbols_data.items():
            if sym in positions:
                continue
            i = d["ts_idx"].get(ts)
            if i is None:
                continue
            trend = d["extras"]["trend"][i]
            heat = d["extras"]["heat"][i]
            if trend < buy_trend or heat < -8.0:
                continue
            if (60.0 + trend + heat + 5.0) < 70.0:
                continue
            raw_signals += 1
            if len(positions) >= max_positions:
                portfolio_full_skips += 1
                continue
            blocked = False
            for fname, fn in filters:
                if not fn(ts):
                    filter_skips[fname] += 1
                    blocked = True
                    break
            if blocked:
                continue
            positions[sym] = {
                "entry_price": d["candles"][i].close, "entry_ts": ts,
                "size_jpy": per_trade_jpy, "bars_held": 0,
            }

        if k % sample_every == 0 or k == len(sorted_ts) - 1:
            unr = 0.0
            for sym, pos in positions.items():
                d = symbols_data[sym]
                # 直近で利用可能な close
                idx = bisect.bisect_right(d["ts_list"], ts) - 1
                if idx >= 0:
                    cur = d["candles"][idx].close
                    unr += (cur / pos["entry_price"] - 1.0) * pos["size_jpy"]
            equity_curve.append((ts, initial_cash + realized + unr))

    # 最終 mark-to-market
    final_unr = 0.0
    if positions:
        last_ts = sorted_ts[-1]
        for sym, pos in positions.items():
            d = symbols_data[sym]
            idx = bisect.bisect_right(d["ts_list"], last_ts) - 1
            if idx >= 0:
                cur = d["candles"][idx].close
                final_unr += (cur / pos["entry_price"] - 1.0) * pos["size_jpy"]

    return {
        "trades": trades, "equity_curve": equity_curve,
        "final_unrealized_jpy": final_unr,
        "initial_cash": initial_cash, "period": period,
        "raw_signals": raw_signals,
        "filter_skips": dict(filter_skips),
        "portfolio_full_skips": portfolio_full_skips,
    }


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------
def analyze(result: dict, period: tuple[float, float]) -> dict[str, Any]:
    trades = result["trades"]
    initial = result["initial_cash"]
    total_net = sum(t["net_pnl_jpy"] for t in trades)
    total_gross = sum(t["gross_pnl_jpy"] for t in trades)
    total_fee = sum(t["fee_jpy"] for t in trades)
    wins = [t for t in trades if t["net_pnl_jpy"] > 0]
    losses = [t for t in trades if t["net_pnl_jpy"] <= 0]

    peak = -float("inf")
    max_dd = 0.0
    for _, eq in result["equity_curve"]:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

    max_streak = 0
    cur = 0
    sorted_trades = sorted(trades, key=lambda t: t["exit_ts"])
    for t in sorted_trades:
        if t["net_pnl_jpy"] <= 0:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 0

    days = (period[1] - period[0]) / 86400.0
    months = days / 30.4375
    tpm = len(trades) / months if months > 0 else 0.0
    pf = (sum(t["net_pnl_jpy"] for t in wins)
          / abs(sum(t["net_pnl_jpy"] for t in losses))) if losses else float("inf")

    by_symbol: dict[str, dict] = defaultdict(lambda: {
        "trades": 0, "wins": 0, "net_pnl": 0.0, "gross_pnl": 0.0,
    })
    for t in trades:
        s = t["symbol"]
        by_symbol[s]["trades"] += 1
        by_symbol[s]["net_pnl"] += t["net_pnl_jpy"]
        by_symbol[s]["gross_pnl"] += t["gross_pnl_jpy"]
        if t["net_pnl_jpy"] > 0:
            by_symbol[s]["wins"] += 1
    for s, d in by_symbol.items():
        d["win_rate_pct"] = d["wins"] / d["trades"] * 100.0 if d["trades"] else 0.0

    return {
        "trades": len(trades),
        "trades_per_month": tpm,
        "total_pnl_net_jpy": total_net + result["final_unrealized_jpy"],
        "total_pnl_net_pct": (total_net + result["final_unrealized_jpy"]) / initial * 100.0,
        "total_pnl_gross_jpy": total_gross + result["final_unrealized_jpy"],
        "total_fees_jpy": total_fee,
        "win_rate_pct": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "avg_win_jpy": sum(t["net_pnl_jpy"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_jpy": sum(t["net_pnl_jpy"] for t in losses) / len(losses) if losses else 0.0,
        "max_drawdown_pct": max_dd * 100.0,
        "profit_factor": pf,
        "longest_losing_streak": max_streak,
        "raw_signals": result["raw_signals"],
        "filter_skips": result["filter_skips"],
        "portfolio_full_skips": result["portfolio_full_skips"],
        "by_symbol": dict(by_symbol),
    }


def monthly_breakdown(result: dict) -> dict[str, dict]:
    per: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0})
    for t in result["trades"]:
        ym = datetime.fromtimestamp(t["exit_ts"], tz=timezone.utc).strftime("%Y-%m")
        per[ym]["pnl"] += t["net_pnl_jpy"]
        per[ym]["n"] += 1
    return dict(per)


# ---------------------------------------------------------------------------
def print_stats(label: str, st: dict) -> None:
    print(f"  [{label:5s}] trades={st['trades']:5d} (raw={st['raw_signals']:5d} "
          f"port_full_skip={st['portfolio_full_skips']:5d})  "
          f"tpm={st['trades_per_month']:5.1f}  "
          f"net={st['total_pnl_net_pct']:+.3f}% (jpy {st['total_pnl_net_jpy']:+.0f})  "
          f"win={st['win_rate_pct']:.1f}%  PF={st['profit_factor']:.2f}  "
          f"DD={st['max_drawdown_pct']:.3f}%  streak={st['longest_losing_streak']}  "
          f"fees={st['total_fees_jpy']:.0f}")
    if st['by_symbol']:
        for s, d in sorted(st['by_symbol'].items()):
            print(f"     {s:8s}  trades={d['trades']:4d} wins={d['wins']:3d} "
                  f"win%={d.get('win_rate_pct', 0):.1f}  net_pnl={d['net_pnl']:+.0f} "
                  f"gross={d['gross_pnl']:+.0f}")
    if st['filter_skips']:
        print(f"     filter_skips: {st['filter_skips']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--market", default="./data/market")
    ap.add_argument("--out", default="./data/backtest/multi")
    ap.add_argument("--symbols", nargs="+", default=["BTC_JPY"])
    ap.add_argument("--max-positions", type=int, default=1)
    ap.add_argument("--per-trade-jpy", type=float, default=10000.0)
    ap.add_argument("--buy-trend", type=float, default=5.0)
    ap.add_argument("--ma-short", type=int, default=5)
    ap.add_argument("--ma-long", type=int, default=20)
    ap.add_argument("--tp-pct", type=float, default=3.5)
    ap.add_argument("--sl-pct", type=float, default=-2.0)
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    ap.add_argument("--filter", action="append", default=[],
                    help="apply named filter: ndx_trend / spx_trend / spx_mom / "
                         "us_hours / events_30 / vix_25")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[load] symbols={args.symbols}", flush=True)
    sd: dict[str, dict] = {}
    for sym in args.symbols:
        d = prepare_symbol(Path(args.data), sym, args.ma_short, args.ma_long)
        if not d:
            print(f"[warn] no data for {sym}", flush=True)
            continue
        sd[sym] = d
        print(f"  {sym}: {len(d['candles'])} bars  "
              f"({datetime.fromtimestamp(d['ts_list'][0], tz=timezone.utc).date()} .. "
              f"{datetime.fromtimestamp(d['ts_list'][-1], tz=timezone.utc).date()})",
              flush=True)

    # フィルター準備
    filter_objs: list[tuple[str, Callable[[float], bool]]] = []
    if args.filter:
        market_dir = Path(args.market)
        spx = load_daily_csv(market_dir / "SPX_d.csv") if (market_dir / "SPX_d.csv").exists() else []
        ndx = load_daily_csv(market_dir / "NDX_d.csv") if (market_dir / "NDX_d.csv").exists() else []
        vix = load_daily_csv(market_dir / "VIX_d.csv") if (market_dir / "VIX_d.csv").exists() else []
        events = generate_events_calendar(2022, 2026)
        registry: dict[str, Callable[[float], bool]] = {
            "us_hours": filter_us_regular_only,
            "spx_trend": make_index_trend_filter(spx, 5, 20) if spx else (lambda t: True),
            "spx_mom": make_index_momentum_filter(spx, 3) if spx else (lambda t: True),
            "ndx_trend": make_index_trend_filter(ndx, 5, 20) if ndx else (lambda t: True),
            "events_30": make_event_avoidance_filter(events, 30, 30),
            "events_60": make_event_avoidance_filter(events, 60, 60),
            "vix_25": make_vix_filter(vix, 25.0) if vix else (lambda t: True),
            "vix_20": make_vix_filter(vix, 20.0) if vix else (lambda t: True),
        }
        for fname in args.filter:
            if fname in registry:
                filter_objs.append((fname, registry[fname]))
            else:
                print(f"[warn] unknown filter: {fname}", flush=True)
        print(f"[filters] {[n for n, _ in filter_objs]}", flush=True)

    # 5 年 train/val/final 分割（過去データに合わせて拡張）
    periods_used = {
        "train": (iso_ts("2024-01-01T00:00:00Z"), iso_ts("2025-06-30T23:45:00Z")),
        "val":   (iso_ts("2025-07-01T00:00:00Z"), iso_ts("2025-12-31T23:45:00Z")),
        "final": (iso_ts("2026-01-01T00:00:00Z"), iso_ts("2026-03-31T23:45:00Z")),
    }
    # BTC のデータが 2022 以降あれば extended_train として 2022-2023 を train_extra に
    if "BTC_JPY" in sd:
        first_bt = sd["BTC_JPY"]["ts_list"][0]
        if first_bt < iso_ts("2022-12-01T00:00:00Z"):
            periods_used["train_extra"] = (
                iso_ts("2022-01-01T00:00:00Z"), iso_ts("2023-12-31T23:45:00Z"),
            )

    print(f"\n[run] params: trend>={args.buy_trend} ma={args.ma_short}/{args.ma_long} "
          f"TP+{args.tp_pct} SL{args.sl_pct} max_pos={args.max_positions} "
          f"per_trade={args.per_trade_jpy:.0f}", flush=True)

    results = {}
    for pname, period in periods_used.items():
        t0 = time.time()
        res = run_multi(
            sd, period,
            buy_trend=args.buy_trend, ma_s=args.ma_short, ma_l=args.ma_long,
            tp_pct=args.tp_pct, sl_pct=args.sl_pct,
            max_positions=args.max_positions,
            per_trade_jpy=args.per_trade_jpy,
            fee_rate=args.fee_rate, filters=filter_objs,
        )
        st = analyze(res, period)
        mo = monthly_breakdown(res)
        results[pname] = {"stats": st, "monthly": mo}
        print(f"\n  ({time.time()-t0:.1f}s) ", end="")
        print_stats(pname, st)

    out = {
        "config": vars(args),
        "results": {k: {"stats": v["stats"], "monthly": v["monthly"]}
                    for k, v in results.items()},
    }
    (out_dir / f"{args.label}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\n[save] {out_dir}/{args.label}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
