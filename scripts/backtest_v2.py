#!/usr/bin/env python3
"""v2 バックテスト: Entry v2 + Exit v2 + p_continue 学習 → 評価。

パイプライン:
  1. BTC 5分足をロード
  2. train 期間で entry を走らせ、各保有中 bar の特徴量+正解ラベルを収集
  3. bucket / logistic の 2 モデルを fit
  4. train / val / final で v2 バックテストを回す (bucket と logistic を別々に)
  5. 結果を比較、月別損益・PF・DD 等を出す

v1 はいじらない。
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from entry_v2 import EntryV2, EntrySignal, atr as atr_fn  # noqa: E402
from exit_v2 import ExitV2, ExitDecision  # noqa: E402
from market_watcher import Candle  # noqa: E402
from p_continue import (  # noqa: E402
    BucketPContinue, LogisticPContinue, Sample,
    build_features, label_hit_upside_first,
)


# ---------------------------------------------------------------------------
# データロード
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


def ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


PERIODS = {
    "train": (ts("2024-01-01T00:00:00Z"), ts("2025-06-30T23:45:00Z")),
    "val":   (ts("2025-07-01T00:00:00Z"), ts("2025-12-31T23:45:00Z")),
    "final": (ts("2026-01-01T00:00:00Z"), ts("2026-03-31T23:45:00Z")),
}


# ---------------------------------------------------------------------------
# サンプル収集（train 期間用）
# ---------------------------------------------------------------------------
def collect_samples(
    cfg: dict, candles: list[Candle], ts_list: list[float],
    period: tuple[float, float],
    up_atr: float = 1.0, down_atr: float = 0.8, horizon_n: int = 8,
) -> list[Sample]:
    """train 期間に Entry v2 を走らせ、保有中の各 bar で正解ラベル付きサンプルを作る。

    exit は「horizon 内に up/down どちらに先に触ったか」。保有は horizon_n 本分で打ち切り。
    """
    entry = EntryV2(cfg)
    samples: list[Sample] = []
    start, end = period
    i_start = bisect.bisect_left(ts_list, start)
    i_end = bisect.bisect_right(ts_list, end)

    i = i_start
    while i < i_end:
        if i < 200:
            i += 1
            continue
        history = candles[max(0, i - 249): i + 1]  # 末尾 250 本に制限（O(N²) 回避）
        sig = entry.evaluate(history)
        if sig.triggered:
            entry_price = candles[i].close
            entry_atr = sig.atr_at_entry
            if entry_atr <= 0:
                i += 1
                continue
            peak = entry_price
            # 保有中の各 bar（entry の次 bar から horizon 分）
            for j in range(1, horizon_n + 1):
                if i + j >= len(candles):
                    break
                sub_history = candles[max(0, i + j - 249): i + j + 1]
                cur_price = sub_history[-1].close
                peak = max(peak, cur_price)
                feat = build_features(
                    entry_price, entry_atr, j, sub_history, peak,
                )
                future = candles[i + j + 1: i + j + 1 + horizon_n]
                lab = label_hit_upside_first(
                    future, cur_price, entry_atr,
                    up_atr=up_atr, down_atr=down_atr, horizon_n=horizon_n,
                )
                if lab is None:
                    continue
                samples.append(Sample(features=feat, label=lab))
            # entry 直後の sample を作るだけで、次 entry までジャンプせず
            # （同一 entry から連続 sample を得るため）
            i += horizon_n  # エントリーが重なると学習データが冗長になるのでスキップ
        else:
            i += 1
    return samples


# ---------------------------------------------------------------------------
# バックテスト本体
# ---------------------------------------------------------------------------
def run_backtest_v2(
    cfg: dict,
    candles: list[Candle],
    ts_list: list[float],
    period: tuple[float, float],
    p_model,
    initial_cash: float = 1_000_000.0,
    per_trade_jpy: float = 10_000.0,
    fee_rate: float = 0.0,
) -> dict[str, Any]:
    entry_mod = EntryV2(cfg)
    exit_mod = ExitV2(cfg, p_model)
    cooldown_bars = int((cfg.get("exit_v2") or {}).get("cooldown_after_exit_bars", 4))

    start, end = period
    i_start = bisect.bisect_left(ts_list, start)
    i_end = bisect.bisect_right(ts_list, end)

    trades: list[dict[str, Any]] = []
    equity_curve: list[tuple[float, float]] = []
    in_position: dict[str, Any] | None = None
    realized_pnl = 0.0
    last_exit_i: int = -10**9  # cooldown 管理

    i = i_start
    while i < i_end:
        if i < 200:
            i += 1
            continue
        history = candles[max(0, i - 249): i + 1]  # 末尾 250 本に制限（O(N²) 回避）
        cur = history[-1]

        if in_position is None:
            # cooldown 中は entry を出さない（エントリー欄に NEW 信号が立っても）
            if i - last_exit_i < cooldown_bars:
                i += 1
                continue
            sig = entry_mod.evaluate(history)
            if sig.triggered:
                in_position = {
                    "entry_price": cur.close,
                    "entry_ts": cur.ts,
                    "entry_atr": sig.atr_at_entry,
                    "bars_held": 0,
                    "peak": cur.close,
                    "entry_reason": sig.reason,
                    "entry_index": i,
                }
        else:
            in_position["bars_held"] += 1
            in_position["peak"] = max(in_position["peak"], cur.high)
            feat = build_features(
                in_position["entry_price"], in_position["entry_atr"],
                in_position["bars_held"], history, in_position["peak"],
            )
            dec = exit_mod.evaluate(
                in_position["entry_price"], in_position["entry_atr"],
                cur.close, in_position["peak"],
                in_position["bars_held"], feat,
            )
            if dec.should_exit:
                gross = (cur.close / in_position["entry_price"] - 1.0) * per_trade_jpy
                fee = per_trade_jpy * fee_rate + (per_trade_jpy + gross) * fee_rate
                trades.append({
                    "entry_ts": in_position["entry_ts"],
                    "exit_ts": cur.ts,
                    "entry_price": in_position["entry_price"],
                    "exit_price": cur.close,
                    "bars_held": in_position["bars_held"],
                    "entry_reason": in_position["entry_reason"],
                    "exit_reason": dec.reason,
                    "p_continue": dec.p_continue,
                    "gross_pnl_jpy": gross,
                    "fee_jpy": fee,
                    "net_pnl_jpy": gross - fee,
                    "gross_pnl_pct": (cur.close / in_position["entry_price"] - 1.0) * 100.0,
                })
                realized_pnl += gross - fee
                in_position = None
                last_exit_i = i

        # equity 記録（1 時間ごと = 12 bar ごと）
        if i % 12 == 0:
            unr = 0.0
            if in_position is not None:
                unr = (cur.close / in_position["entry_price"] - 1.0) * per_trade_jpy
            equity_curve.append((cur.ts, initial_cash + realized_pnl + unr))
        i += 1

    # 未決済は最終 bar の close で mark-to-market（決済はしない）
    final_unrealized = 0.0
    if in_position is not None:
        final_price = candles[i_end - 1].close
        final_unrealized = (final_price / in_position["entry_price"] - 1.0) * per_trade_jpy

    return {
        "trades": trades,
        "final_unrealized_jpy": final_unrealized,
        "equity_curve": equity_curve,
        "open_position": in_position,
        "initial_cash": initial_cash,
        "period": period,
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

    # max drawdown
    max_dd = 0.0
    peak = -float("inf")
    for _, eq in result["equity_curve"]:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)

    # losing streak
    max_streak = 0
    cur_streak = 0
    for t in trades:
        if t["net_pnl_jpy"] <= 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    days = (period[1] - period[0]) / 86400.0
    months = days / 30.4375
    tpm = len(trades) / months if months > 0 else 0.0

    pf = (sum(t["net_pnl_jpy"] for t in wins)
          / abs(sum(t["net_pnl_jpy"] for t in losses))) if losses else float("inf")

    exit_reasons = Counter()
    for t in trades:
        r = t["exit_reason"]
        if r.startswith("hard_stop"):
            exit_reasons["hard_stop"] += 1
        elif r.startswith("trailing_stop"):
            exit_reasons["trailing_stop"] += 1
        elif r.startswith("max_hold"):
            exit_reasons["max_hold"] += 1
        elif r.startswith("low_p_continue"):
            exit_reasons["low_p_continue"] += 1
        elif r.startswith("ev_negative"):
            exit_reasons["ev_negative"] += 1
        else:
            exit_reasons[r] += 1

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
        "exit_reasons": dict(exit_reasons),
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
# デフォルト v2 設定
# ---------------------------------------------------------------------------
DEFAULT_V2_CFG = {
    "entry_v2": {
        "regime_ma_fast": 50,
        "regime_ma_slow": 200,
        "regime_atr_max_pct": 2.0,
        "regime_rsi_max": 78.0,
        "breakout_window_bars": 40,         # 長めにして entry を絞る
        "momentum_min_atr_frac": 0.5,       # 強めの bar 限定
        "quality_rsi_max": 70.0,
        "quality_volume_lookback": 20,
        "quality_volume_min_ratio": 1.3,    # 出来高要求をやや強く
        "quality_peak_distance_pct": 0.3,
        "atr_period": 14,
        "rsi_period": 14,
    },
    "exit_v2": {
        # safety rails (primary exit)
        "hard_stop_pct": 2.5,
        "trailing_stop_pct": 1.2,
        "trailing_activate_atr": 0.5,      # +0.5 ATR 含み益後に trailing 有効化
        "max_hold_bars": 48,
        # EV 判定は secondary。最小保有 bar 経過後のみ
        "min_hold_bars_for_ev": 6,
        "upside_atr": 1.0,
        "downside_atr": 1.0,
        "min_p_continue": 0.35,            # これ未満 AND EV 負なら exit
        "min_ev_atr": -0.15,
        "cooldown_after_exit_bars": 12,    # 60 分クールダウン
    },
}


# ---------------------------------------------------------------------------
def print_summary(label: str, stats: dict, monthly: dict) -> None:
    print(f"\n--- {label} ---")
    print(f"  trades={stats['trades']:4d}  tpm={stats['trades_per_month']:5.1f}  "
          f"net_pnl={stats['total_pnl_net_pct']:+.3f}% ({stats['total_pnl_net_jpy']:+.0f} JPY)  "
          f"fees={stats['total_fees_jpy']:.0f}")
    print(f"  win={stats['win_rate_pct']:.1f}%  PF={stats['profit_factor']:.2f}  "
          f"DD={stats['max_drawdown_pct']:.3f}%  "
          f"longest_losing_streak={stats['longest_losing_streak']}")
    print(f"  avg_win={stats['avg_win_jpy']:+.0f}  avg_loss={stats['avg_loss_jpy']:+.0f}")
    print(f"  exit reasons: {stats['exit_reasons']}")
    if monthly:
        print("  monthly net_pnl:")
        for ym in sorted(monthly):
            m = monthly[ym]
            mark = "" if m["pnl"] >= 0 else "(-)"
            print(f"    {ym}  {m['pnl']:>+7.0f} JPY  n={m['n']:>3} {mark}")


# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--out", default="./data/backtest/v2")
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    # p_continue サンプリング（対称 1.0/1.0 で baseline 0.5）
    ap.add_argument("--up-atr", type=float, default=1.0)
    ap.add_argument("--down-atr", type=float, default=1.0)
    ap.add_argument("--horizon-n", type=int, default=12)
    args = ap.parse_args()

    print("[load] BTC candles", flush=True)
    candles = load_btc_candles(Path(args.data))
    ts_list = [c.ts for c in candles]
    print(f"[load] {len(candles)} candles  "
          f"{datetime.fromtimestamp(ts_list[0], tz=timezone.utc).isoformat()} .. "
          f"{datetime.fromtimestamp(ts_list[-1], tz=timezone.utc).isoformat()}",
          flush=True)

    cfg = DEFAULT_V2_CFG
    # -------- Phase 1: サンプル収集 --------
    print("\n[collect] samples on train period", flush=True)
    t0 = time.time()
    samples = collect_samples(
        cfg, candles, ts_list, PERIODS["train"],
        up_atr=args.up_atr, down_atr=args.down_atr, horizon_n=args.horizon_n,
    )
    label_dist = Counter(s.label for s in samples)
    print(f"[collect] {len(samples)} samples in {time.time()-t0:.1f}s  "
          f"label dist: {dict(label_dist)}",
          flush=True)

    # -------- Phase 2: モデル学習 --------
    bucket = BucketPContinue()
    bucket.fit(samples)
    logit = LogisticPContinue()
    print("[fit] training logistic regression...", flush=True)
    t0 = time.time()
    logit.fit(samples)
    print(f"[fit] done in {time.time()-t0:.1f}s", flush=True)

    # -------- Phase 3: バックテスト実行 --------
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results: dict[str, dict] = {}
    for model_name, model in [("bucket", bucket), ("logistic", logit)]:
        print(f"\n==== Model: {model_name} ====", flush=True)
        model_results = {}
        for pname in ["train", "val", "final"]:
            res = run_backtest_v2(
                cfg, candles, ts_list, PERIODS[pname], model,
                fee_rate=args.fee_rate,
            )
            st = analyze(res, PERIODS[pname])
            mo = monthly_breakdown(res)
            print_summary(f"{model_name} / {pname}", st, mo)
            model_results[pname] = {"stats": st, "monthly": mo,
                                    "trades_count": len(res["trades"])}
        all_results[model_name] = model_results

    # -------- 保存 --------
    (out_dir / "v2_summary.json").write_text(
        json.dumps(all_results, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (out_dir / "bucket_model.json").write_text(
        json.dumps(bucket.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "logistic_model.json").write_text(
        json.dumps(logit.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[save] {out_dir}/v2_summary.json + model files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
