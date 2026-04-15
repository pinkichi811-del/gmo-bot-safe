#!/usr/bin/env python3
"""v3 バックテスト (BTC 専用)。

EntryV3A / EntryV3B を train / val / final で回し、v1 baseline と v2 の既存結果も
含めて比較レポートを出す。

高速化: 5min と 1H それぞれの indicators を precompute しておき、各 bar は O(1) 判定。
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from entry_v3 import EntryV3A, EntryV3B, SharedExit  # noqa: E402
from market_watcher import Candle  # noqa: E402


# ---------------------------------------------------------------------------
# データロード / 集約
# ---------------------------------------------------------------------------
def load_btc_candles(data_root: Path) -> list[Candle]:
    all_c: list[Candle] = []
    for fp in sorted(data_root.glob("BTC_JPY_*.json")):
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


def aggregate_1h(candles_5m: list[Candle]) -> list[Candle]:
    buckets: dict[int, list[Candle]] = {}
    for c in candles_5m:
        hour_ts = int(c.ts // 3600) * 3600
        buckets.setdefault(hour_ts, []).append(c)
    result: list[Candle] = []
    for hk in sorted(buckets):
        bars = buckets[hk]
        if len(bars) < 10:  # 不完全な時間は除外（12 本が理想、10 本以上で許容）
            continue
        result.append(Candle(
            ts=float(hk),
            open=bars[0].open,
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            close=bars[-1].close,
            volume=sum(b.volume for b in bars),
        ))
    return result


# ---------------------------------------------------------------------------
# Indicator 事前計算（高速化の要）
# ---------------------------------------------------------------------------
def precompute_indicators(
    candles: list[Candle], atr_period: int = 14, rsi_period: int = 14,
    sma_short: int = 20, sma_long: int = 50,
    atr_pct_lookback: int = 100, range_window: int = 20,
) -> dict[str, list]:
    n = len(candles)
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    opens_ = [c.open for c in candles]

    # TR
    tr = [0.0] * n
    for i in range(1, n):
        pc = closes[i - 1]
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc))

    # ATR (rolling SMA of TR)
    atr = [0.0] * n
    s = 0.0
    for i in range(n):
        s += tr[i]
        if i >= atr_period:
            s -= tr[i - atr_period]
        if i >= atr_period - 1:
            atr[i] = s / atr_period

    # ATR percentile over last N bars
    atr_pct = [50.0] * n
    for i in range(n):
        if i < atr_period + atr_pct_lookback - 1:
            continue
        window = atr[i - atr_pct_lookback + 1: i + 1]
        cur = atr[i]
        below = 0
        for a in window:
            if a <= cur:
                below += 1
        atr_pct[i] = below / atr_pct_lookback * 100.0

    # SMA short
    sma_s = [0.0] * n
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= sma_short:
            s -= closes[i - sma_short]
        if i >= sma_short - 1:
            sma_s[i] = s / sma_short

    # SMA long
    sma_l = [0.0] * n
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= sma_long:
            s -= closes[i - sma_long]
        if i >= sma_long - 1:
            sma_l[i] = s / sma_long

    # RSI (simple SMA-based)
    rsi = [50.0] * n
    gains_s = 0.0
    losses_s = 0.0
    for i in range(1, n):
        diff = closes[i] - closes[i - 1]
        g = diff if diff > 0 else 0.0
        l_ = -diff if diff < 0 else 0.0
        gains_s += g
        losses_s += l_
        if i >= rsi_period + 1:
            old_diff = closes[i - rsi_period] - closes[i - rsi_period - 1]
            gains_s -= old_diff if old_diff > 0 else 0.0
            losses_s -= -old_diff if old_diff < 0 else 0.0
        if i >= rsi_period:
            avg_g = gains_s / rsi_period
            avg_l = losses_s / rsi_period
            if avg_l == 0:
                rsi[i] = 100.0 if avg_g > 0 else 50.0
            else:
                rs = avg_g / avg_l
                rsi[i] = 100.0 - 100.0 / (1.0 + rs)

    # range width atr (direct 20-bar high-low / atr)
    range_width = [0.0] * n
    for i in range(range_window - 1, n):
        if atr[i] <= 0:
            continue
        hi = max(highs[j] for j in range(i - range_window + 1, i + 1))
        lo = min(lows[j] for j in range(i - range_window + 1, i + 1))
        range_width[i] = (hi - lo) / atr[i]

    # upper wick ratio (current bar)
    upper_wick = [0.0] * n
    for i in range(n):
        rng = highs[i] - lows[i]
        if rng <= 0:
            continue
        body_top = max(closes[i], opens_[i])
        upper_wick[i] = (highs[i] - body_top) / rng

    return {
        "closes": closes, "highs": highs, "lows": lows,
        "atr": atr, "atr_pct": atr_pct,
        "sma20": sma_s, "sma50": sma_l,
        "rsi": rsi, "range_width": range_width,
        "upper_wick": upper_wick,
    }


def build_5m_to_1h_map(ts_5m: list[float], ts_1h: list[float]) -> list[int]:
    """各 5m bar から、直前の完成済み 1H bar の index を返す。"""
    result = [-1] * len(ts_5m)
    j = -1
    n1 = len(ts_1h)
    for i, t in enumerate(ts_5m):
        while j + 1 < n1 and ts_1h[j + 1] + 3600 <= t:
            j += 1
        result[i] = j
    return result


# ---------------------------------------------------------------------------
# バックテスト本体
# ---------------------------------------------------------------------------
def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


PERIODS = {
    "train": (ts("2024-01-01T00:00:00Z"), ts("2025-06-30T23:45:00Z")),
    "val":   (ts("2025-07-01T00:00:00Z"), ts("2025-12-31T23:45:00Z")),
    "final": (ts("2026-01-01T00:00:00Z"), ts("2026-03-31T23:45:00Z")),
}


def run_backtest_v3(
    variant: str, cfg: dict, candles_5m: list[Candle], ind_5m,
    ind_1h, map_5m_to_1h, period: tuple[float, float],
    ts_list_5m: list[float],
    per_trade_jpy: float = 10_000.0, initial_cash: float = 1_000_000.0,
    fee_rate: float = 0.0005,
) -> dict[str, Any]:
    entry_a = EntryV3A(cfg) if variant == "A" else None
    entry_b = EntryV3B(cfg) if variant == "B" else None
    exit_m = SharedExit(cfg)
    cooldown_bars = int((cfg.get("shared_exit") or {}).get("cooldown_after_exit_bars", 12))

    start, end = period
    i_start = max(250, bisect.bisect_left(ts_list_5m, start))
    i_end = bisect.bisect_right(ts_list_5m, end)

    closes = ind_5m["closes"]
    highs = ind_5m["highs"]

    trades: list[dict[str, Any]] = []
    equity_curve: list[tuple[float, float]] = []
    in_pos: dict[str, Any] | None = None
    realized = 0.0
    last_exit_i = -10**9

    for i in range(i_start, i_end):
        cur_ts = candles_5m[i].ts
        cur_close = closes[i]
        cur_high = highs[i]

        if in_pos is None:
            if i - last_exit_i < cooldown_bars:
                continue
            if variant == "A":
                sig = entry_a.evaluate(i, ind_5m)
            else:
                sig = entry_b.evaluate(i, ind_5m, ind_1h, map_5m_to_1h)
            if sig.triggered:
                in_pos = {
                    "entry_price": cur_close,
                    "entry_ts": cur_ts,
                    "entry_atr": sig.atr_at_entry,
                    "bars_held": 0,
                    "peak": cur_close,
                    "entry_reason": sig.reason,
                }
        else:
            in_pos["bars_held"] += 1
            if cur_high > in_pos["peak"]:
                in_pos["peak"] = cur_high
            reason, _ = exit_m.evaluate(
                in_pos["entry_price"], in_pos["entry_atr"],
                cur_close, in_pos["peak"], in_pos["bars_held"],
            )
            if reason is not None:
                gross = (cur_close / in_pos["entry_price"] - 1.0) * per_trade_jpy
                fee = per_trade_jpy * fee_rate + (per_trade_jpy + gross) * fee_rate
                net = gross - fee
                trades.append({
                    "entry_ts": in_pos["entry_ts"],
                    "exit_ts": cur_ts,
                    "entry_price": in_pos["entry_price"],
                    "exit_price": cur_close,
                    "bars_held": in_pos["bars_held"],
                    "entry_reason": in_pos["entry_reason"],
                    "exit_reason": reason,
                    "gross_pnl_jpy": gross,
                    "fee_jpy": fee,
                    "net_pnl_jpy": net,
                })
                realized += net
                in_pos = None
                last_exit_i = i

        if i % 12 == 0:
            unr = 0.0
            if in_pos is not None:
                unr = (cur_close / in_pos["entry_price"] - 1.0) * per_trade_jpy
            equity_curve.append((cur_ts, initial_cash + realized + unr))

    # 未決済 mark-to-market
    final_unrealized = 0.0
    if in_pos is not None:
        final_close = closes[i_end - 1]
        final_unrealized = (final_close / in_pos["entry_price"] - 1.0) * per_trade_jpy

    return {
        "trades": trades, "equity_curve": equity_curve,
        "final_unrealized_jpy": final_unrealized,
        "initial_cash": initial_cash, "period": period,
    }


# ---------------------------------------------------------------------------
# メトリクス
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

    exit_reasons: Counter = Counter()
    for t in trades:
        r = t["exit_reason"]
        if r.startswith("hard_stop"):
            exit_reasons["hard_stop"] += 1
        elif r.startswith("trailing_stop"):
            exit_reasons["trailing_stop"] += 1
        elif r.startswith("max_hold"):
            exit_reasons["max_hold"] += 1
        else:
            exit_reasons[r] += 1

    return {
        "trades": len(trades),
        "trades_per_month": tpm,
        "total_pnl_net_jpy": total_net + result["final_unrealized_jpy"],
        "total_pnl_net_pct": (total_net + result["final_unrealized_jpy"]) / initial * 100.0,
        "total_gross_jpy": total_gross + result["final_unrealized_jpy"],
        "total_fees_jpy": total_fee,
        "win_rate_pct": len(wins) / len(trades) * 100.0 if trades else 0.0,
        "avg_win_jpy": (sum(t["net_pnl_jpy"] for t in wins) / len(wins)) if wins else 0.0,
        "avg_loss_jpy": (sum(t["net_pnl_jpy"] for t in losses) / len(losses)) if losses else 0.0,
        "max_drawdown_pct": max_dd * 100.0,
        "profit_factor": pf,
        "longest_losing_streak": max_streak,
        "exit_reasons": dict(exit_reasons),
        "max_hold_share_pct": (exit_reasons.get("max_hold", 0) / len(trades) * 100.0) if trades else 0.0,
        "days": days,
    }


def monthly_breakdown(result: dict) -> dict[str, dict]:
    per_month: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnl": 0.0, "n": 0})
    for t in result["trades"]:
        ym = datetime.fromtimestamp(t["exit_ts"], tz=timezone.utc).strftime("%Y-%m")
        per_month[ym]["pnl"] += t["net_pnl_jpy"]
        per_month[ym]["n"] += 1
    return dict(per_month)


# ---------------------------------------------------------------------------
# Baseline 結果（既存の run から手入力でコピー）
# ---------------------------------------------------------------------------
BASELINE_RESULTS = {
    "v1 (trend=3 ma=3/10 int=300 TP+3.5 SL-2.0)": {
        "train": {"trades": 283, "trades_per_month": 15.7,
                  "total_pnl_net_pct": 1.337, "win_rate_pct": 46.6,
                  "profit_factor": 1.38, "max_drawdown_pct": 0.054,
                  "longest_losing_streak": 7, "max_hold_share_pct": None,
                  "exit_reasons": {"stop_loss": 166, "take_profit": 117}},
        "val":   {"trades": 46, "trades_per_month": 7.6,
                  "total_pnl_net_pct": 0.058, "win_rate_pct": 41.3,
                  "profit_factor": 1.10, "max_drawdown_pct": 0.054,
                  "longest_losing_streak": 6, "max_hold_share_pct": None,
                  "exit_reasons": {"stop_loss": 28, "take_profit": 18}},
        "final": {"trades": 52, "trades_per_month": 17.6,
                  "total_pnl_net_pct": -0.193, "win_rate_pct": 32.7,
                  "profit_factor": 0.75, "max_drawdown_pct": 0.054,
                  "longest_losing_streak": 8, "max_hold_share_pct": None,
                  "exit_reasons": {"stop_loss": 35, "take_profit": 17}},
    },
    "v2 (EntryV2 + ExitV2 bucket model)": {
        "train": {"trades": 1098, "trades_per_month": 61.1,
                  "total_pnl_net_pct": -0.757, "win_rate_pct": 41.2,
                  "profit_factor": 0.82, "max_drawdown_pct": 0.958,
                  "longest_losing_streak": 13, "max_hold_share_pct": 75.7,
                  "exit_reasons": {"max_hold": 831, "trailing_stop": 255, "hard_stop": 12}},
        "val":   {"trades": 353, "trades_per_month": 58.4,
                  "total_pnl_net_pct": -0.385, "win_rate_pct": 39.7,
                  "profit_factor": 0.65, "max_drawdown_pct": 0.422,
                  "longest_losing_streak": 8, "max_hold_share_pct": 83.3,
                  "exit_reasons": {"max_hold": 294, "trailing_stop": 57, "hard_stop": 2}},
        "final": {"trades": 157, "trades_per_month": 53.1,
                  "total_pnl_net_pct": -0.140, "win_rate_pct": 37.6,
                  "profit_factor": 0.78, "max_drawdown_pct": 0.171,
                  "longest_losing_streak": 14, "max_hold_share_pct": 68.2,
                  "exit_reasons": {"max_hold": 107, "trailing_stop": 50}},
    },
}


# ---------------------------------------------------------------------------
# デフォルト v3 設定
# ---------------------------------------------------------------------------
DEFAULT_V3_CFG = {
    "entry_v3a": {
        "atr_pct_max": 30.0,
        "range_width_atr_max": 4.0,
        "upper_wick_ratio_max": 0.2,
        "rsi_max": 70.0,
    },
    "entry_v3b": {
        "htf_rsi_min": 50.0,
        "atr_pct_max": 40.0,
        "require_1h_above_sma50": True,
        "require_5m_above_sma20": True,
    },
    "shared_exit": {
        "hard_stop_pct": 2.5,
        "trailing_stop_pct": 1.2,
        "trailing_activate_atr": 0.5,
        "max_hold_bars": 48,
        "cooldown_after_exit_bars": 12,
    },
}


# ---------------------------------------------------------------------------
def print_stats(label: str, st: dict) -> None:
    ex = st.get("exit_reasons", {})
    print(f"  [{label}] trades={st['trades']:5d}  tpm={st['trades_per_month']:5.1f}  "
          f"net_pnl={st['total_pnl_net_pct']:+.3f}%  "
          f"win={st['win_rate_pct']:.1f}%  PF={st['profit_factor']:.2f}  "
          f"DD={st['max_drawdown_pct']:.3f}%  streak={st['longest_losing_streak']}  "
          f"max_hold={st.get('max_hold_share_pct', 0.0) or 0.0:.1f}%  "
          f"avg_win={st.get('avg_win_jpy', 0):+.0f} avg_loss={st.get('avg_loss_jpy', 0):+.0f}")
    if ex:
        print(f"    exits: {dict(ex)}")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--out", default="./data/backtest/v3")
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[load] candles", flush=True)
    candles_5m = load_btc_candles(Path(args.data))
    ts_list_5m = [c.ts for c in candles_5m]
    print(f"[load] 5m: {len(candles_5m)} candles", flush=True)

    print("[aggregate] building 1H", flush=True)
    candles_1h = aggregate_1h(candles_5m)
    ts_list_1h = [c.ts for c in candles_1h]
    print(f"[aggregate] 1h: {len(candles_1h)} candles", flush=True)

    print("[precompute] 5m indicators", flush=True)
    t0 = time.time()
    ind_5m = precompute_indicators(
        candles_5m, atr_period=14, rsi_period=14,
        sma_short=20, sma_long=50, atr_pct_lookback=100, range_window=20,
    )
    print(f"[precompute] 5m done in {time.time()-t0:.1f}s", flush=True)

    print("[precompute] 1h indicators", flush=True)
    t0 = time.time()
    ind_1h = precompute_indicators(
        candles_1h, atr_period=14, rsi_period=14,
        sma_short=20, sma_long=50, atr_pct_lookback=100, range_window=20,
    )
    print(f"[precompute] 1h done in {time.time()-t0:.1f}s", flush=True)

    map_5m_to_1h = build_5m_to_1h_map(ts_list_5m, ts_list_1h)

    # ========== v3-A ==========
    print("\n==== v3-A (compression-only) ====", flush=True)
    cfg = DEFAULT_V3_CFG
    v3a_results = {}
    for pname in ("train", "val", "final"):
        t1 = time.time()
        r = run_backtest_v3("A", cfg, candles_5m, ind_5m, ind_1h,
                            map_5m_to_1h, PERIODS[pname], ts_list_5m,
                            fee_rate=args.fee_rate)
        st = analyze(r, PERIODS[pname])
        mo = monthly_breakdown(r)
        v3a_results[pname] = {"stats": st, "monthly": mo, "trades": r["trades"]}
        print(f"  ({time.time()-t1:.1f}s)", end=" ")
        print_stats(f"v3-A / {pname}", st)

    # ========== v3-B ==========
    print("\n==== v3-B (MTF + compression) ====", flush=True)
    v3b_results = {}
    for pname in ("train", "val", "final"):
        t1 = time.time()
        r = run_backtest_v3("B", cfg, candles_5m, ind_5m, ind_1h,
                            map_5m_to_1h, PERIODS[pname], ts_list_5m,
                            fee_rate=args.fee_rate)
        st = analyze(r, PERIODS[pname])
        mo = monthly_breakdown(r)
        v3b_results[pname] = {"stats": st, "monthly": mo, "trades": r["trades"]}
        print(f"  ({time.time()-t1:.1f}s)", end=" ")
        print_stats(f"v3-B / {pname}", st)

    # ========== 比較 ==========
    all_results = {
        **BASELINE_RESULTS,
        "v3-A (compression-only)": {p: v3a_results[p]["stats"] for p in ("train", "val", "final")},
        "v3-B (MTF + compression)": {p: v3b_results[p]["stats"] for p in ("train", "val", "final")},
    }

    # 月別 & trades は v3 のみ保存（baseline は summary のみ）
    (out_dir / "v3_summary.json").write_text(
        json.dumps({
            "v3a": v3a_results, "v3b": v3b_results,
            "baselines": BASELINE_RESULTS,
            "comparison": all_results,
        }, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # CSV
    with (out_dir / "v3_comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "strategy", "period", "trades", "tpm", "net_pnl_pct",
            "win_rate_pct", "profit_factor", "max_drawdown_pct",
            "longest_losing_streak", "max_hold_share_pct",
        ])
        for strat, periods in all_results.items():
            for p, st in periods.items():
                w.writerow([
                    strat, p, st["trades"],
                    round(st["trades_per_month"], 2),
                    round(st["total_pnl_net_pct"], 3),
                    round(st["win_rate_pct"], 1),
                    round(st["profit_factor"], 2) if st["profit_factor"] != float("inf") else "inf",
                    round(st["max_drawdown_pct"], 3),
                    st["longest_losing_streak"],
                    round(st["max_hold_share_pct"], 1) if st.get("max_hold_share_pct") is not None else "N/A",
                ])

    # Markdown report
    md = ["# v3 バックテスト比較レポート\n"]
    md.append("## 設定サマリ")
    md.append(f"- 期間: train 2024-01-01..2025-06-30 / val 2025-07-01..2025-12-31 / final 2026-01-01..2026-03-31")
    md.append(f"- 手数料: {args.fee_rate*100:.3f}%/片側  対象: BTC_JPY のみ  1ポジのみ  per_trade_jpy=10,000")
    md.append(f"- shared_exit: hard_stop {cfg['shared_exit']['hard_stop_pct']}% / trailing {cfg['shared_exit']['trailing_stop_pct']}% (活性化 +{cfg['shared_exit']['trailing_activate_atr']} ATR) / max_hold {cfg['shared_exit']['max_hold_bars']} bars / cooldown {cfg['shared_exit']['cooldown_after_exit_bars']} bars\n")
    md.append("## 比較表 (train / val / final)\n")
    md.append("| 戦略 | period | trades | tpm | net_pnl | win | PF | maxDD | streak | max_hold% |")
    md.append("|---|---|---|---|---|---|---|---|---|---|")
    for strat, periods in all_results.items():
        for p, st in periods.items():
            pf = st["profit_factor"]
            md.append(
                f"| {strat} | {p} | {st['trades']} | {st['trades_per_month']:.1f} | "
                f"{st['total_pnl_net_pct']:+.3f}% | {st['win_rate_pct']:.1f}% | "
                f"{pf:.2f} | {st['max_drawdown_pct']:.3f}% | "
                f"{st['longest_losing_streak']} | "
                f"{(st.get('max_hold_share_pct') or 0.0):.1f}% |"
            )
    md.append("")

    md.append("## v3-A 月別 (train+val+final 連結)\n")
    md.append("| month | trades | net_pnl (JPY) |")
    md.append("|---|---|---|")
    merged_mo_a: dict[str, dict[str, Any]] = {}
    for p in ("train", "val", "final"):
        for ym, d in v3a_results[p]["monthly"].items():
            if ym in merged_mo_a:
                merged_mo_a[ym]["pnl"] += d["pnl"]
                merged_mo_a[ym]["n"] += d["n"]
            else:
                merged_mo_a[ym] = dict(d)
    for ym in sorted(merged_mo_a):
        d = merged_mo_a[ym]
        md.append(f"| {ym} | {d['n']} | {d['pnl']:+.0f} |")
    md.append("")

    md.append("## v3-B 月別\n")
    md.append("| month | trades | net_pnl (JPY) |")
    md.append("|---|---|---|")
    merged_mo_b: dict[str, dict[str, Any]] = {}
    for p in ("train", "val", "final"):
        for ym, d in v3b_results[p]["monthly"].items():
            if ym in merged_mo_b:
                merged_mo_b[ym]["pnl"] += d["pnl"]
                merged_mo_b[ym]["n"] += d["n"]
            else:
                merged_mo_b[ym] = dict(d)
    for ym in sorted(merged_mo_b):
        d = merged_mo_b[ym]
        md.append(f"| {ym} | {d['n']} | {d['pnl']:+.0f} |")
    md.append("")

    # 2026-03 単月のハイライト
    md.append("## 2026-03 単月ハイライト\n")
    md.append("| 戦略 | trades | net_pnl (JPY) |")
    md.append("|---|---|---|")
    for name, mo in [("v3-A", merged_mo_a), ("v3-B", merged_mo_b)]:
        d = mo.get("2026-03", {"pnl": 0.0, "n": 0})
        md.append(f"| {name} | {d['n']} | {d['pnl']:+.0f} |")
    md.append("")

    (out_dir / "v3_REPORT.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n[save] {out_dir}/v3_summary.json, v3_comparison.csv, v3_REPORT.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
