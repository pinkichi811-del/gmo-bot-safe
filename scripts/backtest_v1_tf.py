#!/usr/bin/env python3
"""v1 Pattern A を高時間足に移行して signal quality を比較する。

同じロジック (MA差 trend signal + TP/SL) を以下で回す:
  - 5min  (baseline)
  - 15min
  - 1H

各時間足で小さなパラメータグリッドを試す:
  - trend threshold: [3, 5, 8]
  - MA (short, long): [(3, 10), (5, 20)]
  - TP/SL: 固定で +3.5 / -2.0
  - cooldown: 0, max_hold なし (v1 Pattern A オリジナルと同じ exit)
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

from market_watcher import Candle  # noqa: E402
from backtest_v3 import load_btc_candles, PERIODS  # noqa: E402


# ---------------------------------------------------------------------------
# 時間足集約
# ---------------------------------------------------------------------------
def aggregate(candles_5m: list[Candle], tf_minutes: int) -> list[Candle]:
    """5min candle を任意の上位足に集約する。"""
    if tf_minutes == 5:
        return candles_5m
    buckets: dict[int, list[Candle]] = {}
    sec = tf_minutes * 60
    for c in candles_5m:
        bt = int(c.ts // sec) * sec
        buckets.setdefault(bt, []).append(c)
    result: list[Candle] = []
    expected = tf_minutes // 5
    min_bars = max(1, int(expected * 0.6))  # 60% 充足で採用
    for bt in sorted(buckets):
        bars = buckets[bt]
        if len(bars) < min_bars:
            continue
        result.append(Candle(
            ts=float(bt),
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
        ))
    return result


# ---------------------------------------------------------------------------
# MA / trend / heat の precompute
# ---------------------------------------------------------------------------
def precompute_extras(
    candles: list[Candle], ma_short: int, ma_long: int,
) -> dict[str, list[float]]:
    n = len(candles)
    closes = [c.close for c in candles]
    opens_ = [c.open for c in candles]

    def rolling_mean(period: int) -> list[float]:
        arr = [0.0] * n
        s = 0.0
        for i in range(n):
            s += closes[i]
            if i >= period:
                s -= closes[i - period]
            if i >= period - 1:
                arr[i] = s / period
        return arr

    ma_s = rolling_mean(ma_short)
    ma_l = rolling_mean(ma_long)

    trend = [0.0] * n
    for i in range(n):
        if ma_l[i] <= 0:
            continue
        ratio = (ma_s[i] - ma_l[i]) / ma_l[i]
        trend[i] = max(-1.0, min(1.0, ratio / 0.05)) * 30.0

    heat = [0.0] * n
    for i in range(n):
        if i < 5:
            continue
        open_px = opens_[i - 4]
        if open_px <= 0:
            continue
        chg = (closes[i] - open_px) / open_px
        if chg > 0.05:
            base = -20.0 * min(chg / 0.10, 1.0)
        elif chg < -0.05:
            base = -10.0 * min(abs(chg) / 0.10, 1.0)
        else:
            base = 5.0
        heat[i] = base
    return {"ma_s": ma_s, "ma_l": ma_l, "trend": trend, "heat": heat}


def entry_fires(i: int, trend: list[float], heat: list[float], buy_trend: float) -> bool:
    if trend[i] < buy_trend:
        return False
    if heat[i] < -8.0:
        return False
    # total >= 70 (60 base + trend + heat + cash_bonus 5 + liq ~0)
    return (60.0 + trend[i] + heat[i] + 5.0) >= 70.0


# ---------------------------------------------------------------------------
# バックテスト
# ---------------------------------------------------------------------------
def run_bt(
    candles: list[Candle], ts_list: list[float], period: tuple[float, float],
    buy_trend: float, ma_short: int, ma_long: int,
    tp_pct: float = 3.5, sl_pct: float = -2.0,
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

    for i in range(i_start, i_end):
        cur_close = closes[i]
        cur_ts = candles[i].ts

        if pos is None:
            if entry_fires(i, trend, heat, buy_trend):
                pos = {
                    "entry_price": cur_close, "entry_ts": cur_ts,
                    "bars_held": 0,
                }
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

        # equity サンプル: 時間足に合わせて間引く
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
    }


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
    for t in trades:
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

    reasons: Counter = Counter()
    for t in trades:
        reasons[t["exit_reason"]] += 1

    return {
        "trades": len(trades),
        "trades_per_month": tpm,
        "total_pnl_net_jpy": total_net + result["final_unrealized_jpy"],
        "total_pnl_net_pct": (total_net + result["final_unrealized_jpy"]) / initial * 100.0,
        "total_gross_jpy": total_gross + result["final_unrealized_jpy"],
        "total_fees_jpy": total_fee,
        "win_rate_pct": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "avg_win_jpy": sum(t["net_pnl_jpy"] for t in wins) / len(wins) if wins else 0.0,
        "avg_loss_jpy": sum(t["net_pnl_jpy"] for t in losses) / len(losses) if losses else 0.0,
        "avg_hold_bars": sum(t["bars_held"] for t in trades) / len(trades) if trades else 0.0,
        "max_drawdown_pct": max_dd * 100.0,
        "profit_factor": pf,
        "longest_losing_streak": max_streak,
        "exit_reasons": dict(reasons),
    }


def monthly_breakdown(result: dict) -> dict[str, dict]:
    per: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0})
    for t in result["trades"]:
        ym = datetime.fromtimestamp(t["exit_ts"], tz=timezone.utc).strftime("%Y-%m")
        per[ym]["pnl"] += t["net_pnl_jpy"]
        per[ym]["n"] += 1
    return dict(per)


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--out", default="./data/backtest/v1_tf")
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[load] candles", flush=True)
    candles_5m = load_btc_candles(Path(args.data))
    print(f"[load] 5m: {len(candles_5m)} candles", flush=True)

    # 時間足データ
    tfs = {
        "5min":  (5, candles_5m),
        "15min": (15, aggregate(candles_5m, 15)),
        "1H":    (60, aggregate(candles_5m, 60)),
    }
    for name, (tf_min, cands) in tfs.items():
        print(f"[tf] {name}: {len(cands)} candles", flush=True)

    # パラメータグリッド
    configs = []
    for buy_trend in [3, 5, 8]:
        for ma_s, ma_l in [(3, 10), (5, 20)]:
            configs.append({"buy_trend": buy_trend, "ma_s": ma_s, "ma_l": ma_l,
                            "tp_pct": 3.5, "sl_pct": -2.0})

    all_results: dict[str, dict] = {}
    for tf_name, (tf_min, cands) in tfs.items():
        ts_list = [c.ts for c in cands]
        for cfg in configs:
            label = (f"{tf_name} trend>={cfg['buy_trend']} "
                     f"ma={cfg['ma_s']}/{cfg['ma_l']} "
                     f"TP+{cfg['tp_pct']}/SL{cfg['sl_pct']}")
            print(f"\n==== {label} ====", flush=True)
            all_results[label] = {}
            for pname in ("train", "val", "final"):
                t0 = time.time()
                res = run_bt(
                    cands, ts_list, PERIODS[pname],
                    buy_trend=cfg["buy_trend"],
                    ma_short=cfg["ma_s"], ma_long=cfg["ma_l"],
                    tp_pct=cfg["tp_pct"], sl_pct=cfg["sl_pct"],
                    fee_rate=args.fee_rate,
                )
                st = analyze(res, PERIODS[pname])
                mo = monthly_breakdown(res)
                all_results[label][pname] = {"stats": st, "monthly": mo}
                print(f"  ({time.time()-t0:.1f}s)  [{pname:5s}] "
                      f"trades={st['trades']:4d}  tpm={st['trades_per_month']:5.1f}  "
                      f"net={st['total_pnl_net_pct']:+.3f}%  "
                      f"win={st['win_rate_pct']:.1f}%  PF={st['profit_factor']:.2f}  "
                      f"DD={st['max_drawdown_pct']:.3f}%  streak={st['longest_losing_streak']}  "
                      f"avg_hold={st['avg_hold_bars']:.0f}  "
                      f"avg_win={st['avg_win_jpy']:+.0f} "
                      f"avg_loss={st['avg_loss_jpy']:+.0f}  "
                      f"exits={st['exit_reasons']}")

    # 保存
    (out_dir / "results.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    with (out_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "label", "period", "trades", "tpm", "net_pnl_pct",
            "win_rate_pct", "profit_factor", "max_drawdown_pct",
            "longest_losing_streak", "avg_hold_bars",
            "avg_win_jpy", "avg_loss_jpy",
        ])
        for label, periods in all_results.items():
            for p, d in periods.items():
                st = d["stats"]
                pf = st["profit_factor"]
                w.writerow([
                    label, p, st["trades"],
                    round(st["trades_per_month"], 2),
                    round(st["total_pnl_net_pct"], 3),
                    round(st["win_rate_pct"], 1),
                    round(pf, 2) if pf != float("inf") else "inf",
                    round(st["max_drawdown_pct"], 3),
                    st["longest_losing_streak"],
                    round(st["avg_hold_bars"], 1),
                    round(st["avg_win_jpy"], 0),
                    round(st["avg_loss_jpy"], 0),
                ])
    print(f"\n[save] {out_dir}/results.json, comparison.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
