#!/usr/bin/env python3
"""v1 Pattern A の entry を固定して、exit を複数パターン比較。

entry（絶対に変えない）:
  - trend >= 3  （MA3/MA10 の比率から算出）
  - heat >= -8
  - BTC 専用 (buy_liquidity=0)
  - cooldown_min = 0
  - max_positions = 1

exit 変種:
  - baseline: TP +3.5% / SL -2.0%（v1 Pattern A のまま）
  - max_hold_24 / max_hold_12: 保有上限を追加
  - early_failure_6 / early_failure_12: N bars 内に +0.5 ATR に届かなければ撤退
  - break_even: 一度 +0.5 ATR 進んでから失速で建値付近 exit
  - partial_tp: +1 ATR で半分利確、残りは trailing
  - momentum_exit: 直近 3 bar 高値更新無し + 建値割れで exit
  - combo_A: max_hold_24 + early_failure_6 + break_even

出力: v3 と同じフォーマットの比較レポート。
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from market_watcher import Candle  # noqa: E402
from backtest_v3 import (  # noqa: E402
    load_btc_candles, precompute_indicators, ts as iso_ts, PERIODS,
)


# ---------------------------------------------------------------------------
# v1 entry に必要な追加 indicators（MA3, MA10, heat 計算用）
# ---------------------------------------------------------------------------
def precompute_v1_extras(
    candles: list[Candle], ma_short: int = 3, ma_long: int = 10,
) -> dict[str, list]:
    n = len(candles)
    closes = [c.close for c in candles]
    opens_ = [c.open for c in candles]
    # MA short
    ma_s = [0.0] * n
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= ma_short:
            s -= closes[i - ma_short]
        if i >= ma_short - 1:
            ma_s[i] = s / ma_short
    # MA long
    ma_l = [0.0] * n
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= ma_long:
            s -= closes[i - ma_long]
        if i >= ma_long - 1:
            ma_l[i] = s / ma_long
    # trend_score: matches scorer._trend_score with ratio_clamp=0.05, max_magnitude=30
    trend = [0.0] * n
    for i in range(n):
        if ma_l[i] <= 0:
            continue
        ratio = (ma_s[i] - ma_l[i]) / ma_l[i]
        trend[i] = max(-1.0, min(1.0, ratio / 0.05)) * 30.0

    # heat_score: scorer._heat_score with window=5, thresholds=±5%
    # = base_heat + rsi_adj. base_heat piecewise:
    #   chg>+5% → -20 * min(chg/0.10, 1)
    #   chg<-5% → -10 * min(|chg|/0.10, 1)
    #   else → +5
    # rsi_adj: RSI>75 → -5 max、RSI<25 → +3 max（現行 config 値）
    # for check, we just need heat >= -8 (almost always true), so compute
    heat = [0.0] * n
    rsi = [50.0] * n  # reuse this for heat rsi adjust
    # Simple RSI already done in ind dict (rsi from precompute_indicators)
    # Here just compute base_heat, assume rsi_adj small
    for i in range(n):
        if i < 5:
            continue
        open_px = opens_[i - 4]
        chg = (closes[i] - open_px) / open_px if open_px > 0 else 0.0
        if chg > 0.05:
            base = -20.0 * min(chg / 0.10, 1.0)
        elif chg < -0.05:
            base = -10.0 * min(abs(chg) / 0.10, 1.0)
        else:
            base = 5.0
        heat[i] = base  # rsi_adj 加算は別途（ここでは省略で概算）

    return {"ma3": ma_s, "ma10": ma_l, "trend": trend, "heat": heat}


def v1_entry_fires(i: int, ind: dict, extras: dict, buy_trend: float = 3.0) -> bool:
    """v1 Pattern A の buy_candidate 判定を簡易再現。"""
    t = extras["trend"][i]
    h = extras["heat"][i]
    if t < buy_trend:
        return False
    if h < -8.0:
        return False
    # total >= 70 チェック: base(60) + trend + liq(~0) + heat + vol(0) + dup(0) + cash(~5)
    # BTC 単独で cash_ratio ~1.0 なので cash_bonus=5（現 config の high_threshold=0.5 >= cash_ratio）
    total_approx = 60.0 + t + h + 5.0
    if total_approx < 70.0:
        return False
    return True


# ---------------------------------------------------------------------------
# Position & Exit variants
# ---------------------------------------------------------------------------
@dataclass
class V1Position:
    entry_i: int
    entry_price: float
    entry_ts: float
    entry_atr: float
    initial_size_jpy: float
    remaining_size_jpy: float
    peak_price: float
    bars_held: int = 0
    partial_profit_jpy: float = 0.0
    partial_taken: bool = False


class ExitVariant:
    name: str = "abstract"

    def check(
        self, pos: V1Position, i: int, closes: list, highs: list,
        atr_arr: list,
    ) -> tuple[str | None, float]:
        """Returns (reason, exit_price). If reason is None, hold.
        exit_price is the price used for booking PnL."""
        raise NotImplementedError


def _check_tp_sl(
    pos: V1Position, close: float, tp_pct: float, sl_pct: float,
) -> tuple[str | None, float]:
    pct = (close - pos.entry_price) / pos.entry_price * 100.0
    if pct <= sl_pct:
        return ("stop_loss", close)
    if pct >= tp_pct:
        return ("take_profit", close)
    return (None, close)


class ExitBaseline(ExitVariant):
    name = "baseline"

    def __init__(self, tp_pct: float = 3.5, sl_pct: float = -2.0) -> None:
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    def check(self, pos, i, closes, highs, atr_arr):
        return _check_tp_sl(pos, closes[i], self.tp_pct, self.sl_pct)


class ExitMaxHold(ExitVariant):
    def __init__(self, max_hold: int, tp_pct: float = 3.5, sl_pct: float = -2.0) -> None:
        self.name = f"max_hold_{max_hold}"
        self.max_hold = max_hold
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    def check(self, pos, i, closes, highs, atr_arr):
        r, px = _check_tp_sl(pos, closes[i], self.tp_pct, self.sl_pct)
        if r:
            return r, px
        if pos.bars_held >= self.max_hold:
            return ("max_hold", closes[i])
        return (None, closes[i])


class ExitEarlyFailure(ExitVariant):
    def __init__(
        self, check_at_bars: int, atr_threshold: float = 0.5,
        tp_pct: float = 3.5, sl_pct: float = -2.0, max_hold: int = 48,
    ) -> None:
        self.name = f"early_failure_{check_at_bars}"
        self.check_bars = check_at_bars
        self.atr_threshold = atr_threshold
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold = max_hold

    def check(self, pos, i, closes, highs, atr_arr):
        r, px = _check_tp_sl(pos, closes[i], self.tp_pct, self.sl_pct)
        if r:
            return r, px
        # early failure
        if pos.bars_held >= self.check_bars and pos.entry_atr > 0:
            peak_atr = (pos.peak_price - pos.entry_price) / pos.entry_atr
            if peak_atr < self.atr_threshold:
                return ("early_failure", closes[i])
        if pos.bars_held >= self.max_hold:
            return ("max_hold", closes[i])
        return (None, closes[i])


class ExitBreakEven(ExitVariant):
    name = "break_even"

    def __init__(
        self, activate_atr: float = 0.5, tolerance_pct: float = 0.1,
        tp_pct: float = 3.5, sl_pct: float = -2.0, max_hold: int = 48,
    ) -> None:
        self.activate_atr = activate_atr
        self.tolerance_pct = tolerance_pct  # 建値から ±this % に戻ったら exit
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold = max_hold

    def check(self, pos, i, closes, highs, atr_arr):
        c = closes[i]
        r, px = _check_tp_sl(pos, c, self.tp_pct, self.sl_pct)
        if r:
            return r, px
        if pos.entry_atr > 0:
            peak_atr = (pos.peak_price - pos.entry_price) / pos.entry_atr
            if peak_atr >= self.activate_atr:
                be_line = pos.entry_price * (1.0 + self.tolerance_pct / 100.0)
                if c <= be_line:
                    return ("break_even", c)
        if pos.bars_held >= self.max_hold:
            return ("max_hold", c)
        return (None, c)


class ExitPartialTP(ExitVariant):
    name = "partial_tp"

    def __init__(
        self, partial_atr: float = 1.0, partial_fraction: float = 0.5,
        trailing_stop_pct: float = 1.5, tp_pct: float = 3.5,
        sl_pct: float = -2.0, max_hold: int = 48,
    ) -> None:
        self.partial_atr = partial_atr
        self.partial_fraction = partial_fraction
        self.trailing_stop_pct = trailing_stop_pct
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold = max_hold

    def check(self, pos, i, closes, highs, atr_arr):
        c = closes[i]
        h = highs[i]
        # partial trigger (using high for intrabar)
        if not pos.partial_taken and pos.entry_atr > 0:
            partial_price = pos.entry_price + pos.entry_atr * self.partial_atr
            if h >= partial_price:
                # book partial profit at partial_price
                half_size = pos.initial_size_jpy * self.partial_fraction
                gain = (partial_price / pos.entry_price - 1.0) * half_size
                pos.partial_profit_jpy += gain
                pos.remaining_size_jpy = pos.initial_size_jpy - half_size
                pos.partial_taken = True
        # SL / TP on close
        pct = (c - pos.entry_price) / pos.entry_price * 100.0
        if pct <= self.sl_pct:
            return ("stop_loss", c)
        if pct >= self.tp_pct:
            return ("take_profit", c)
        # after partial, trailing
        if pos.partial_taken:
            retrace_pct = (c - pos.peak_price) / pos.peak_price * 100.0
            if retrace_pct <= -self.trailing_stop_pct:
                return ("trailing_stop", c)
        if pos.bars_held >= self.max_hold:
            return ("max_hold", c)
        return (None, c)


class ExitMomentum(ExitVariant):
    name = "momentum_exit"

    def __init__(
        self, lookback: int = 3, tp_pct: float = 3.5, sl_pct: float = -2.0,
        max_hold: int = 48,
    ) -> None:
        self.lookback = lookback
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct
        self.max_hold = max_hold

    def check(self, pos, i, closes, highs, atr_arr):
        c = closes[i]
        r, px = _check_tp_sl(pos, c, self.tp_pct, self.sl_pct)
        if r:
            return r, px
        if pos.bars_held >= self.lookback + 1:
            # 直近 `lookback` bar で新高値更新なし + 建値割れ
            recent_highs = highs[i - self.lookback: i]
            cur_high = highs[i]
            no_new_high = cur_high < max(recent_highs)
            below_entry = c < pos.entry_price
            if no_new_high and below_entry:
                return ("momentum_failure", c)
        if pos.bars_held >= self.max_hold:
            return ("max_hold", c)
        return (None, c)


class ExitCombined(ExitVariant):
    name = "combo_A"

    def __init__(
        self, max_hold: int = 24, early_failure_bars: int = 6,
        early_failure_atr: float = 0.5, be_activate_atr: float = 0.5,
        be_tolerance_pct: float = 0.1,
        tp_pct: float = 3.5, sl_pct: float = -2.0,
    ) -> None:
        self.max_hold = max_hold
        self.ef_bars = early_failure_bars
        self.ef_atr = early_failure_atr
        self.be_activate = be_activate_atr
        self.be_tol = be_tolerance_pct
        self.tp_pct = tp_pct
        self.sl_pct = sl_pct

    def check(self, pos, i, closes, highs, atr_arr):
        c = closes[i]
        r, px = _check_tp_sl(pos, c, self.tp_pct, self.sl_pct)
        if r:
            return r, px
        # early failure
        if pos.bars_held >= self.ef_bars and pos.entry_atr > 0:
            peak_atr = (pos.peak_price - pos.entry_price) / pos.entry_atr
            if peak_atr < self.ef_atr:
                return ("early_failure", c)
        # break even (once activated)
        if pos.entry_atr > 0:
            peak_atr = (pos.peak_price - pos.entry_price) / pos.entry_atr
            if peak_atr >= self.be_activate:
                be_line = pos.entry_price * (1.0 + self.be_tol / 100.0)
                if c <= be_line:
                    return ("break_even", c)
        if pos.bars_held >= self.max_hold:
            return ("max_hold", c)
        return (None, c)


# ---------------------------------------------------------------------------
# バックテスト
# ---------------------------------------------------------------------------
def run_backtest(
    ind: dict, extras: dict, candles: list[Candle], ts_list: list[float],
    period: tuple[float, float], exit_v: ExitVariant,
    per_trade_jpy: float = 10_000.0, initial_cash: float = 1_000_000.0,
    fee_rate: float = 0.0005,
) -> dict[str, Any]:
    closes = ind["closes"]
    highs = ind["highs"]
    atr_arr = ind["atr"]

    start, end = period
    i_start = max(250, bisect.bisect_left(ts_list, start))
    i_end = bisect.bisect_right(ts_list, end)

    trades: list[dict[str, Any]] = []
    equity_curve: list[tuple[float, float]] = []
    pos: V1Position | None = None
    realized_net = 0.0

    for i in range(i_start, i_end):
        cur_ts = candles[i].ts
        cur_close = closes[i]
        cur_high = highs[i]

        if pos is None:
            if v1_entry_fires(i, ind, extras):
                pos = V1Position(
                    entry_i=i, entry_price=cur_close,
                    entry_ts=cur_ts, entry_atr=atr_arr[i],
                    initial_size_jpy=per_trade_jpy,
                    remaining_size_jpy=per_trade_jpy,
                    peak_price=cur_close,
                )
        else:
            pos.bars_held += 1
            if cur_high > pos.peak_price:
                pos.peak_price = cur_high
            reason, exit_price = exit_v.check(pos, i, closes, highs, atr_arr)
            if reason is not None:
                # gross P/L on remaining size
                rem_gross = (exit_price / pos.entry_price - 1.0) * pos.remaining_size_jpy
                total_gross = rem_gross + pos.partial_profit_jpy
                # fees: buy fee on initial, partial fee on partial portion (if taken), sell fee on remaining proceeds
                buy_fee = pos.initial_size_jpy * fee_rate
                sell_fee_rem = (pos.remaining_size_jpy + rem_gross) * fee_rate
                # partial fee (if taken): partial portion × fee applied once for "partial sell"
                partial_fee = 0.0
                if pos.partial_taken:
                    partial_portion = pos.initial_size_jpy - pos.remaining_size_jpy
                    partial_fee = (partial_portion + pos.partial_profit_jpy) * fee_rate
                total_fee = buy_fee + sell_fee_rem + partial_fee
                net = total_gross - total_fee
                trades.append({
                    "entry_ts": pos.entry_ts,
                    "exit_ts": cur_ts,
                    "entry_price": pos.entry_price,
                    "exit_price": exit_price,
                    "bars_held": pos.bars_held,
                    "exit_reason": reason,
                    "gross_pnl_jpy": total_gross,
                    "fee_jpy": total_fee,
                    "net_pnl_jpy": net,
                    "partial_taken": pos.partial_taken,
                    "partial_profit_jpy": pos.partial_profit_jpy,
                })
                realized_net += net
                pos = None

        if i % 12 == 0:
            unr = 0.0
            if pos is not None:
                unr = (cur_close / pos.entry_price - 1.0) * pos.remaining_size_jpy
                unr += pos.partial_profit_jpy
            equity_curve.append((cur_ts, initial_cash + realized_net + unr))

    # 未決済 mark-to-market
    final_unr = 0.0
    if pos is not None:
        final_close = closes[i_end - 1]
        final_unr = (final_close / pos.entry_price - 1.0) * pos.remaining_size_jpy
        final_unr += pos.partial_profit_jpy

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

    exit_reasons: Counter = Counter()
    for t in trades:
        exit_reasons[t["exit_reason"]] += 1

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
        "max_drawdown_pct": max_dd * 100.0,
        "profit_factor": pf,
        "longest_losing_streak": max_streak,
        "exit_reasons": dict(exit_reasons),
        "max_hold_share_pct": (exit_reasons.get("max_hold", 0) / len(trades) * 100.0) if trades else 0.0,
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
    ap.add_argument("--out", default="./data/backtest/v1_exits")
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[load] candles", flush=True)
    candles = load_btc_candles(Path(args.data))
    ts_list = [c.ts for c in candles]
    print(f"[load] {len(candles)} candles", flush=True)

    print("[precompute] indicators", flush=True)
    t0 = time.time()
    ind = precompute_indicators(
        candles, atr_period=14, rsi_period=14,
        sma_short=20, sma_long=50, atr_pct_lookback=100, range_window=20,
    )
    extras = precompute_v1_extras(candles, ma_short=3, ma_long=10)
    print(f"[precompute] done in {time.time()-t0:.1f}s", flush=True)

    # 変種リスト
    variants: list[ExitVariant] = [
        ExitBaseline(tp_pct=3.5, sl_pct=-2.0),  # baseline
        ExitMaxHold(max_hold=48),               # 比較用: max_hold 導入
        ExitMaxHold(max_hold=24),
        ExitMaxHold(max_hold=12),
        ExitEarlyFailure(check_at_bars=6),
        ExitEarlyFailure(check_at_bars=12),
        ExitBreakEven(activate_atr=0.5, tolerance_pct=0.1),
        ExitPartialTP(partial_atr=1.0, partial_fraction=0.5, trailing_stop_pct=1.5),
        ExitMomentum(lookback=3),
        ExitCombined(max_hold=24, early_failure_bars=6, early_failure_atr=0.5),
    ]

    all_results: dict[str, dict] = {}
    for v in variants:
        print(f"\n==== {v.name} ====", flush=True)
        all_results[v.name] = {}
        for pname in ("train", "val", "final"):
            r = run_backtest(ind, extras, candles, ts_list, PERIODS[pname], v,
                             fee_rate=args.fee_rate)
            st = analyze(r, PERIODS[pname])
            mo = monthly_breakdown(r)
            all_results[v.name][pname] = {"stats": st, "monthly": mo}
            print(f"  [{pname:5s}] trades={st['trades']:5d}  "
                  f"tpm={st['trades_per_month']:5.1f}  "
                  f"net={st['total_pnl_net_pct']:+.3f}%  "
                  f"win={st['win_rate_pct']:.1f}%  "
                  f"PF={st['profit_factor']:.2f}  "
                  f"DD={st['max_drawdown_pct']:.3f}%  "
                  f"streak={st['longest_losing_streak']}  "
                  f"max_hold={st['max_hold_share_pct']:.1f}%")
            if st["trades"]:
                print(f"    exits: {st['exit_reasons']}")

    # save json
    (out_dir / "results.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    # save csv
    with (out_dir / "comparison.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "variant", "period", "trades", "tpm", "net_pnl_pct",
            "win_rate_pct", "profit_factor", "max_drawdown_pct",
            "longest_losing_streak", "max_hold_pct",
            "avg_win", "avg_loss",
        ])
        for name, periods in all_results.items():
            for p, d in periods.items():
                st = d["stats"]
                pf = st["profit_factor"]
                w.writerow([
                    name, p, st["trades"],
                    round(st["trades_per_month"], 2),
                    round(st["total_pnl_net_pct"], 3),
                    round(st["win_rate_pct"], 1),
                    round(pf, 2) if pf != float("inf") else "inf",
                    round(st["max_drawdown_pct"], 3),
                    st["longest_losing_streak"],
                    round(st["max_hold_share_pct"], 1),
                    round(st["avg_win_jpy"], 0),
                    round(st["avg_loss_jpy"], 0),
                ])
    print(f"\n[save] {out_dir}/results.json, comparison.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
