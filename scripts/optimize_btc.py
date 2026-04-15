#!/usr/bin/env python3
"""BTC 専用・複数年バックテストによる頻度帯別グリッドサーチ。

前提:
  - 監視銘柄 = BTC_JPY のみ
  - max_positions = 1（BTC 単独）
  - 判定順序・ロジックは現行 src/ を流用
  - 期間は train/val/final の 3 分割で過剰最適化を避ける

二段探索:
  Phase A: 入口側（trend threshold × MA × score_interval）のグリッド
  Phase B: Phase A の頻度帯別トップから TP/SL/cooldown を探索

出力:
  data/backtest/optimize/phaseA.json
  data/backtest/optimize/phaseB_band_{low,mid,high}.json
  data/backtest/optimize/final.md
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_watcher import Candle, MarketSnapshot, Ticker  # noqa: E402
from risk_guard import Decision, RiskGuard  # noqa: E402
from scorer import Scorer, apply_cash_bonus  # noqa: E402
from state_store import Position, StateStore  # noqa: E402


# ---------------------------------------------------------------------------
class BacktestStateStore(StateStore):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.sim_time: float = 0.0

    def set_cooldown(self, symbol: str, minutes: float) -> None:
        self._state.setdefault("cooldown_until", {})[symbol] = (
            self.sim_time + minutes * 60.0
        )

    def in_cooldown(self, symbol: str) -> bool:
        until = float((self._state.get("cooldown_until") or {}).get(symbol, 0.0))
        return self.sim_time < until

    def cooldown_remaining_sec(self, symbol: str) -> float:
        until = float((self._state.get("cooldown_until") or {}).get(symbol, 0.0))
        return max(0.0, until - self.sim_time)


# ---------------------------------------------------------------------------
# 全期間 BTC データを一度だけロード
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


# ---------------------------------------------------------------------------
def build_snapshot(
    t: float,
    candles: list[Candle],
    ts_list: list[float],
    n_window: int = 30,
) -> MarketSnapshot | None:
    idx = bisect.bisect_right(ts_list, t) - 1
    if idx < 0:
        return None
    window = candles[max(0, idx - n_window + 1): idx + 1]
    if not window:
        return None
    latest = window[-1]
    tk = Ticker(
        symbol="BTC_JPY", last=latest.close,
        bid=latest.close, ask=latest.close,
        volume=latest.volume, ts=latest.ts,
    )
    return MarketSnapshot(
        ts=t, tickers={"BTC_JPY": tk}, ohlcv={"BTC_JPY": window},
    )


# ---------------------------------------------------------------------------
def make_cfg(base_cfg: dict, overrides: dict[str, Any]) -> dict:
    """overrides は flat dict: 'buy_trend', 'ma_short', 'ma_long', 'take_profit_pct',
       'stop_loss_pct', 'cooldown_min', 'score_interval_sec', 'max_positions' 等。"""
    cfg = deepcopy(base_cfg)
    # BTC only
    cfg["symbols"] = {"core": ["BTC_JPY"], "satellite": []}
    # max_positions
    cfg.setdefault("limits", {})["max_positions"] = overrides.get("max_positions", 1)
    # scorer
    sc = cfg.setdefault("scorer", {})
    thr = sc.setdefault("thresholds", {}).setdefault("buy_candidate", {})
    thr["trend"] = float(overrides.get("buy_trend", thr.get("trend", 18)))
    # BTC の出来高は BTC 単位で小さく、現 volume_divisor では liquidity score が
    # 常に 0 付近になり "liquidity >= 10" 閾値で全候補が弾かれる。
    # BTC 単独運用では流動性は常に十分という前提で閾値を 0 に下げる（実質無効化）。
    # ※ overrides に buy_liquidity が無い場合は強制的に 0 にする（BTC 専用モードの既定）。
    thr["liquidity"] = float(overrides.get("buy_liquidity", 0.0))
    # heat もディフォ -8 のままで BTC には緩い
    tr = sc.setdefault("trend", {})
    tr["short_ma"] = int(overrides.get("ma_short", tr.get("short_ma", 5)))
    tr["long_ma"] = int(overrides.get("ma_long", tr.get("long_ma", 20)))
    # exits
    ex = cfg.setdefault("exits", {})
    ex["take_profit_pct"] = float(overrides.get("take_profit_pct",
                                                ex.get("take_profit_pct", 6.0)))
    ex["stop_loss_pct"] = float(overrides.get("stop_loss_pct",
                                              ex.get("stop_loss_pct", -4.0)))
    ex["cooldown_min"] = float(overrides.get("cooldown_min",
                                             ex.get("cooldown_min", 180)))
    # loop
    cfg.setdefault("loop", {})["score_interval_sec"] = int(
        overrides.get("score_interval_sec",
                      cfg.get("loop", {}).get("score_interval_sec", 900))
    )
    # risk
    cfg.setdefault("risk", {})["stop_file"] = "/__nonexistent__/STOP"
    return cfg


# ---------------------------------------------------------------------------
def run_one_backtest(
    cfg: dict,
    candles: list[Candle],
    ts_list: list[float],
    start_ts: float,
    end_ts: float,
    collect_trades: bool = True,
) -> dict[str, Any]:
    """指定期間の BTC 専用バックテストを 1 本走らせる。"""
    initial_cash = float((cfg.get("portfolio") or {}).get("initial_cash_jpy", 1_000_000))
    cooldown_min = float(cfg.get("exits", {}).get("cooldown_min", 180))
    tick_sec = int(cfg.get("loop", {}).get("score_interval_sec", 900))

    with tempfile.TemporaryDirectory() as td:
        state = BacktestStateStore(path=str(Path(td) / "state.json"))
        scorer = Scorer(cfg)
        guard = RiskGuard(cfg, state)

        trades: list[dict[str, Any]] = []
        equity_curve: list[tuple[float, float]] = []  # (t, equity)
        sample_every = max(1, 3600 // tick_sec)       # equity は 1 時間ごとに記録

        t = start_ts
        i = 0
        while t <= end_ts:
            state.sim_time = t

            if not state.is_halted():
                snap = build_snapshot(t, candles, ts_list)
                if snap is not None and snap.tickers:
                    if guard.health_check(snap):
                        scores = scorer.score(snap, held_symbols=state.positions().keys())
                        # compute_equity_faithful
                        pos_value = 0.0
                        used_cash = 0.0
                        for sym, pos in state.positions().items():
                            tk = snap.tickers.get(sym)
                            cur = tk.last if tk else pos.entry_price
                            if pos.entry_price > 0:
                                units = pos.size_jpy / pos.entry_price
                                pos_value += units * cur
                            used_cash += pos.size_jpy
                        cash = max(initial_cash - used_cash, 0.0)
                        equity = cash + pos_value
                        cash_ratio = (cash / equity) if equity > 0 else 0.0
                        scores = apply_cash_bonus(scores, cash_ratio, cfg)

                        sells = guard.evaluate_sells(snap)
                        passed, verdicts = guard.evaluate_buy_candidates(scores)
                        buys, port_rej = guard.apply_portfolio_constraints(
                            passed, snap, cash_jpy=cash, total_equity_jpy=equity,
                        )

                        # sell 実行
                        for d in sells:
                            pos = state.positions().get(d.symbol)
                            if pos is None or pos.entry_price <= 0:
                                continue
                            units = pos.size_jpy / pos.entry_price
                            proceeds = units * d.price_ref
                            gross = proceeds - pos.size_jpy
                            if collect_trades:
                                trades.append({
                                    "symbol": d.symbol,
                                    "entry_price": pos.entry_price,
                                    "exit_price": d.price_ref,
                                    "size_jpy": pos.size_jpy,
                                    "entry_ts": pos.entry_ts,
                                    "exit_ts": t,
                                    "duration_min": (t - pos.entry_ts) / 60.0,
                                    "close_reason": d.reason,
                                    "gross_pnl_jpy": gross,
                                    "gross_pnl_pct": gross / pos.size_jpy * 100.0,
                                })
                            else:
                                trades.append({
                                    "exit_ts": t,
                                    "close_reason_type": (
                                        "take_profit" if d.reason.startswith("take_profit")
                                        else "stop_loss"
                                    ),
                                    "gross_pnl_jpy": gross,
                                    "size_jpy": pos.size_jpy,
                                })
                            state.remove_position(d.symbol)
                            state.set_cooldown(d.symbol, minutes=cooldown_min)

                        # buy 実行
                        for d in buys:
                            state.set_position(Position(
                                symbol=d.symbol, size_jpy=d.size_jpy,
                                entry_price=d.price_ref, entry_ts=t,
                            ))

                        if i % sample_every == 0:
                            # 最新 equity を記録
                            pv = 0.0
                            uc = 0.0
                            for sym, pos in state.positions().items():
                                tk = snap.tickers.get(sym)
                                cur = tk.last if tk else pos.entry_price
                                if pos.entry_price > 0:
                                    pv += (pos.size_jpy / pos.entry_price) * cur
                                uc += pos.size_jpy
                            eq_now = max(initial_cash - uc, 0.0) + pv
                            equity_curve.append((t, eq_now))

            t += tick_sec
            i += 1

        # 最終評価（未決済は未実現扱い）
        final_snap = build_snapshot(end_ts, candles, ts_list)
        final_unrealized = 0.0
        open_positions = []
        if final_snap is not None:
            for sym, pos in state.positions().items():
                tk = final_snap.tickers.get(sym)
                cur = tk.last if tk else pos.entry_price
                units = pos.size_jpy / pos.entry_price
                unr = units * cur - pos.size_jpy
                final_unrealized += unr
                open_positions.append({
                    "symbol": sym, "entry_price": pos.entry_price,
                    "size_jpy": pos.size_jpy, "current_price": cur,
                    "unrealized_pnl_jpy": unr,
                })

        return {
            "trades": trades,
            "final_unrealized_jpy": final_unrealized,
            "equity_curve": equity_curve,
            "open_positions": open_positions,
            "initial_cash": initial_cash,
        }


# ---------------------------------------------------------------------------
def analyze_result(
    result: dict, start_ts: float, end_ts: float, fee_rate: float = 0.0,
) -> dict[str, Any]:
    """1 回の run 結果を要約指標にする。"""
    trades = result["trades"]
    initial = result["initial_cash"]
    total_gross = 0.0
    total_fee = 0.0
    wins = 0
    losses = 0
    win_sum = 0.0
    loss_sum = 0.0
    sl_count = 0
    tp_count = 0
    for tr in trades:
        gross = tr["gross_pnl_jpy"]
        size = tr["size_jpy"]
        fee = (size + size + gross) * fee_rate  # buy_fee + sell_fee
        net = gross - fee
        total_gross += gross
        total_fee += fee
        if net > 0:
            wins += 1
            win_sum += net
        else:
            losses += 1
            loss_sum += net  # negative
        reason = tr.get("close_reason") or tr.get("close_reason_type", "")
        if reason.startswith("take_profit"):
            tp_count += 1
        elif reason.startswith("stop_loss"):
            sl_count += 1

    total_net = total_gross - total_fee
    final_unrealized = result["final_unrealized_jpy"]
    # 期間日数
    days = (end_ts - start_ts) / 86400.0
    months = days / 30.4375
    trades_per_month = len(trades) / months if months > 0 else 0.0

    # max drawdown（gross equity ベース）
    curve = result["equity_curve"]
    max_dd = 0.0
    peak = -float("inf")
    for _, eq in curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

    # profit factor
    pf = (win_sum / abs(loss_sum)) if loss_sum < 0 else (float("inf") if win_sum > 0 else 0.0)

    # win rate (net)
    wr = (wins / len(trades) * 100.0) if trades else 0.0

    # avg pnl per trade
    avg_pnl = (total_net / len(trades)) if trades else 0.0
    avg_win = (win_sum / wins) if wins else 0.0
    avg_loss = (loss_sum / losses) if losses else 0.0

    # longest losing streak
    max_streak = 0
    cur_streak = 0
    for tr in trades:
        gross = tr["gross_pnl_jpy"]
        fee = (tr["size_jpy"] * 2 + gross) * fee_rate
        net = gross - fee
        if net <= 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    return {
        "trades": len(trades),
        "trades_per_month": trades_per_month,
        "total_pnl_gross_jpy": total_gross + final_unrealized,
        "total_pnl_net_jpy": total_net + final_unrealized,
        "total_pnl_gross_pct": (total_gross + final_unrealized) / initial * 100.0,
        "total_pnl_net_pct": (total_net + final_unrealized) / initial * 100.0,
        "total_fees_jpy": total_fee,
        "win_rate_pct": wr,
        "avg_pnl_per_trade_jpy": avg_pnl,
        "avg_win_jpy": avg_win,
        "avg_loss_jpy": avg_loss,
        "max_drawdown_pct": max_dd * 100.0,
        "profit_factor": pf,
        "stop_loss_count": sl_count,
        "take_profit_count": tp_count,
        "longest_losing_streak": max_streak,
        "days": days,
        "final_unrealized_jpy": final_unrealized,
    }


def yearly_pnl(result: dict) -> dict[int, float]:
    """closed trades の各年損益 (JPY)."""
    per_year: dict[int, float] = defaultdict(float)
    for tr in result["trades"]:
        y = datetime.fromtimestamp(tr["exit_ts"], tz=timezone.utc).year
        per_year[y] += tr["gross_pnl_jpy"]
    return dict(per_year)


def monthly_stats(result: dict) -> dict[str, dict]:
    per_month: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnl": 0.0, "trades": 0})
    for tr in result["trades"]:
        ym = datetime.fromtimestamp(tr["exit_ts"], tz=timezone.utc).strftime("%Y-%m")
        per_month[ym]["pnl"] += tr["gross_pnl_jpy"]
        per_month[ym]["trades"] += 1
    return dict(per_month)


# ---------------------------------------------------------------------------
# 評価期間と composite スコア
# ---------------------------------------------------------------------------
def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


PERIODS = {
    "train": (ts("2024-01-01T00:00:00Z"), ts("2025-06-30T23:45:00Z")),
    "val":   (ts("2025-07-01T00:00:00Z"), ts("2025-12-31T23:45:00Z")),
    "final": (ts("2026-01-01T00:00:00Z"), ts("2026-03-31T23:45:00Z")),
}


def composite_score(train: dict, val: dict) -> float:
    """実運用向き評価: train と val でどちらも耐えているか。"""
    t_pnl = train["total_pnl_net_pct"]
    v_pnl = val["total_pnl_net_pct"]
    t_dd = train["max_drawdown_pct"]
    v_dd = val["max_drawdown_pct"]
    avg_pnl = (t_pnl + v_pnl) / 2.0
    stability = -abs(t_pnl - v_pnl) * 0.3  # 期間間ばらつきペナルティ
    dd_pen = -max(t_dd, v_dd) * 0.4
    return avg_pnl + stability + dd_pen


# ---------------------------------------------------------------------------
# 頻度帯定義
# ---------------------------------------------------------------------------
BANDS = {
    "low":  (15, 40),    # おすすめ帯
    "mid":  (70, 130),   # 100 前後
    "high": (150, 260),  # 200 前後
}


def classify_band(tpm: float) -> str | None:
    for name, (lo, hi) in BANDS.items():
        if lo <= tpm <= hi:
            return name
    return None


# ---------------------------------------------------------------------------
# Phase A: 入口グリッド
# ---------------------------------------------------------------------------
def phase_a(
    base_cfg: dict,
    candles: list[Candle],
    ts_list: list[float],
    fee_rate: float,
    out_dir: Path,
) -> list[dict[str, Any]]:
    # Phase A は入口の頻度特性を測る。出口は最小単位（TP+2/SL-2/cooldown=0）で
    # 保有を最短化し、「入口がどれだけ発火するか」を純粋に見る。
    phase_a_exits = {
        "take_profit_pct": 2.0,
        "stop_loss_pct": -2.0,
        "cooldown_min": 0,
    }
    grid = []
    # trend を広く: 観察から 6 以上では全く届かないので 1〜10 にも広げる
    for trend in [1, 2, 3, 4, 5, 6, 8, 10]:
        for ma_short, ma_long in [(3, 10), (5, 20), (5, 15), (3, 15), (3, 8), (5, 10)]:
            for interval in [300, 900]:
                grid.append({
                    "buy_trend": trend,
                    "ma_short": ma_short, "ma_long": ma_long,
                    "score_interval_sec": interval,
                    **phase_a_exits,
                })
    print(f"[phaseA] {len(grid)} combos (exits fixed at TP+2/SL-2/cooldown=0)")

    results = []
    t0 = time.time()
    for i, ov in enumerate(grid):
        cfg = make_cfg(base_cfg, ov)
        train_res = run_one_backtest(
            cfg, candles, ts_list, *PERIODS["train"], collect_trades=False,
        )
        val_res = run_one_backtest(
            cfg, candles, ts_list, *PERIODS["val"], collect_trades=False,
        )
        train_stats = analyze_result(train_res, *PERIODS["train"], fee_rate=fee_rate)
        val_stats = analyze_result(val_res, *PERIODS["val"], fee_rate=fee_rate)
        # trades_per_month は train/val の和で判断（より安定）
        total_trades = train_stats["trades"] + val_stats["trades"]
        total_months = (train_stats["days"] + val_stats["days"]) / 30.4375
        tpm_combined = total_trades / total_months if total_months > 0 else 0
        comp = composite_score(train_stats, val_stats)
        row = {
            "override": ov,
            "train": train_stats,
            "val": val_stats,
            "trades_per_month_combined": tpm_combined,
            "band": classify_band(tpm_combined),
            "composite": comp,
        }
        results.append(row)
        elapsed = time.time() - t0
        avg = elapsed / (i + 1)
        eta = avg * (len(grid) - i - 1)
        if (i + 1) % 5 == 0 or i == len(grid) - 1:
            print(f"  [{i+1:3d}/{len(grid)}] trend={ov['buy_trend']} "
                  f"ma={ov['ma_short']}/{ov['ma_long']} int={ov['score_interval_sec']}  "
                  f"tpm={tpm_combined:6.1f}  comp={comp:+.2f}  "
                  f"elapsed={elapsed:.1f}s  eta={eta:.1f}s",
                  flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "phaseA.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return results


# ---------------------------------------------------------------------------
# Phase B: exit スイープ（各頻度帯の top 入口に対して）
# ---------------------------------------------------------------------------
def phase_b(
    base_cfg: dict,
    candles: list[Candle],
    ts_list: list[float],
    seed_overrides: list[dict],
    band_label: str,
    fee_rate: float,
    out_dir: Path,
) -> list[dict[str, Any]]:
    grid = []
    for tp in [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
        for sl in [-1.5, -2.0, -2.5, -3.0, -3.5, -4.0, -5.0]:
            grid.append({"take_profit_pct": tp, "stop_loss_pct": sl})

    all_rows = []
    t0 = time.time()
    total_runs = len(seed_overrides) * len(grid)
    run_idx = 0
    for seed in seed_overrides:
        for ex in grid:
            ov = {**seed, **ex}
            cfg = make_cfg(base_cfg, ov)
            train_res = run_one_backtest(
                cfg, candles, ts_list, *PERIODS["train"], collect_trades=False,
            )
            val_res = run_one_backtest(
                cfg, candles, ts_list, *PERIODS["val"], collect_trades=False,
            )
            train_stats = analyze_result(train_res, *PERIODS["train"], fee_rate=fee_rate)
            val_stats = analyze_result(val_res, *PERIODS["val"], fee_rate=fee_rate)
            total_trades = train_stats["trades"] + val_stats["trades"]
            total_months = (train_stats["days"] + val_stats["days"]) / 30.4375
            tpm = total_trades / total_months if total_months > 0 else 0
            comp = composite_score(train_stats, val_stats)
            all_rows.append({
                "override": ov, "train": train_stats, "val": val_stats,
                "trades_per_month_combined": tpm, "composite": comp,
                "band": classify_band(tpm),
            })
            run_idx += 1
            if run_idx % 10 == 0 or run_idx == total_runs:
                elapsed = time.time() - t0
                eta = elapsed / run_idx * (total_runs - run_idx)
                print(f"  [phaseB {band_label} {run_idx:3d}/{total_runs}] "
                      f"tp={ex['take_profit_pct']:.1f} sl={ex['stop_loss_pct']:.1f} "
                      f"tpm={tpm:6.1f} comp={comp:+.2f}  "
                      f"elapsed={elapsed:.1f}s eta={eta:.1f}s",
                      flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"phaseB_{band_label}.json").write_text(
        json.dumps(all_rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return all_rows


# ---------------------------------------------------------------------------
# Final evaluation: 最終候補を final 期間でも検証
# ---------------------------------------------------------------------------
def final_evaluate(
    base_cfg: dict,
    candles: list[Candle],
    ts_list: list[float],
    overrides: dict,
    fee_rate: float,
) -> dict[str, Any]:
    cfg = make_cfg(base_cfg, overrides)
    per_period = {}
    combined_trades = []
    for name, (start, end) in PERIODS.items():
        res = run_one_backtest(cfg, candles, ts_list, start, end, collect_trades=True)
        stats = analyze_result(res, start, end, fee_rate=fee_rate)
        per_period[name] = {
            "stats": stats,
            "yearly_pnl": yearly_pnl(res),
            "monthly": monthly_stats(res),
        }
        combined_trades.extend(res["trades"])
    return {
        "override": overrides,
        "per_period": per_period,
        "total_trades_collected": len(combined_trades),
    }


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--out", default="./data/backtest/optimize")
    ap.add_argument("--config", default="./config/app.yaml")
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    ap.add_argument("--skip-phase-a", action="store_true")
    ap.add_argument("--skip-phase-b", action="store_true")
    args = ap.parse_args()

    base_cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    candles = load_btc_candles(Path(args.data))
    ts_list = [c.ts for c in candles]
    print(f"[load] BTC candles: {len(candles)}  "
          f"range: {datetime.fromtimestamp(ts_list[0], tz=timezone.utc).isoformat()} "
          f"- {datetime.fromtimestamp(ts_list[-1], tz=timezone.utc).isoformat()}",
          flush=True)

    out_dir = Path(args.out)

    # ========= Phase A =========
    if not args.skip_phase_a:
        print("\n=========== Phase A: 入口グリッド ===========", flush=True)
        phase_a_results = phase_a(base_cfg, candles, ts_list, args.fee_rate, out_dir)
    else:
        phase_a_results = json.loads(
            (out_dir / "phaseA.json").read_text(encoding="utf-8")
        )

    # 頻度帯ごとの seed を選ぶ（Phase A composite 上位）
    by_band: dict[str, list[dict]] = {b: [] for b in BANDS}
    for row in phase_a_results:
        if row["band"] in by_band:
            by_band[row["band"]].append(row)
    for b in BANDS:
        by_band[b].sort(key=lambda r: r["composite"], reverse=True)

    print("\n[phaseA] band sizes:",
          {b: len(v) for b, v in by_band.items()})
    for b, rows in by_band.items():
        print(f"  [{b}] top seeds:")
        for r in rows[:3]:
            ov = r["override"]
            print(f"    trend={ov['buy_trend']} ma={ov['ma_short']}/{ov['ma_long']} "
                  f"int={ov['score_interval_sec']}  "
                  f"tpm={r['trades_per_month_combined']:5.1f}  "
                  f"comp={r['composite']:+.2f}")

    # ========= Phase B: 各帯上位 2 シード × TP/SL =========
    best_by_band: dict[str, dict] = {}
    if not args.skip_phase_b:
        for b in BANDS:
            seeds = [row["override"] for row in by_band[b][:2]]
            if not seeds:
                print(f"[phaseB {b}] no seeds, skip")
                continue
            print(f"\n=========== Phase B: {b} ({len(seeds)} seeds) ===========",
                  flush=True)
            rows = phase_b(base_cfg, candles, ts_list, seeds, b,
                           args.fee_rate, out_dir)
            # 同じ頻度帯に留まったものだけから選ぶ
            in_band = [r for r in rows if r["band"] == b]
            if not in_band:
                # 帯を外れてもその seed の中で最良を拾う
                in_band = rows
            in_band.sort(key=lambda r: r["composite"], reverse=True)
            best_by_band[b] = in_band[0]

    # ========= 最終候補 final 期間評価 =========
    print("\n=========== Final evaluation ===========", flush=True)
    finals: dict[str, dict] = {}
    for b, row in best_by_band.items():
        print(f"[final] evaluating best of {b}: {row['override']}", flush=True)
        finals[b] = final_evaluate(
            base_cfg, candles, ts_list, row["override"], args.fee_rate,
        )

    (out_dir / "final.json").write_text(
        json.dumps({
            "phaseA_top_per_band": {b: by_band[b][:3] for b in BANDS},
            "phaseB_best_per_band": best_by_band,
            "final_evaluation": finals,
        }, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("\n[save]", out_dir / "final.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
