#!/usr/bin/env python3
"""ローカル大量試行最適化エントリポイント。

使い方:
  # スモーク (~1 分)
  python scripts/optimize_local.py --trials 20 --workers 2

  # 標準 (想定 8〜12 分)
  python scripts/optimize_local.py --trials 1000 --workers 6

  # 大規模 (想定 40〜80 分)
  python scripts/optimize_local.py --trials 10000 --workers 12 --stage2-k 30
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_v1_tf import load_btc_candles  # noqa: E402
from regime_filter import load_daily_csv, generate_events_calendar  # noqa: E402

from market_watcher import Candle  # noqa: E402
import json as _json


def load_symbol_candles(data_root: Path, symbol: str) -> list:
    """任意シンボルの 5min JSON を全て読んで Candle list を返す。"""
    all_c: list = []
    for fp in sorted(data_root.glob(f"{symbol}_*.json")):
        payload = _json.loads(fp.read_text(encoding="utf-8"))
        for c in payload["candles"]:
            all_c.append(Candle(
                ts=int(c["openTime"]) / 1000.0,
                open=float(c["open"]), high=float(c["high"]),
                low=float(c["low"]), close=float(c["close"]),
                volume=float(c["volume"]),
            ))
    all_c.sort(key=lambda x: x.ts)
    return all_c

from optimize_common import (  # noqa: E402
    CSV_COLUMNS, PERIOD_NAMES, _init_worker, _run_trial, param_key,
)


# ---------------------------------------------------------------------------
# パラメータ空間定義
# ---------------------------------------------------------------------------
SPACE = {
    # Multi-symbol
    "symbols": [["BTC"], ["BTC", "ETH"], ["BTC", "ETH", "XRP"]],
    "max_positions": [1, 2, 3],
    # Core entry
    "buy_trend": list(range(3, 16)),
    "ma_short": list(range(3, 11)),
    "ma_long": list(range(15, 51)),
    # Exit
    "tp_pct": [round(x * 0.5, 1) for x in range(3, 17)],   # 1.5..8.0
    "sl_pct": [round(-x * 0.5, 1) for x in range(2, 9)],   # -1.0..-4.0
    "max_hold_bars": [0, 24, 48, 96, 288, 576],  # 0=無制限, 288≈24h@5min
    "trail_pct": [0.0, 1.0, 1.5, 2.0, 3.0, 4.0],
    "cooldown_min": [0, 30, 60, 180, 360],
    # Regime filters
    "use_ndx": [True, False],
    "ndx_ma_short": [3, 5, 10],
    "ndx_ma_long": [10, 20, 50],
    "use_spx": [True, False],
    "spx_ma_short": [3, 5, 10],
    "spx_ma_long": [10, 20, 50],
    "use_vix": [True, False],
    "vix_max": [15.0, 20.0, 25.0, 30.0],
    "use_us_hours": [True, False],
    "use_events": [True, False],
    "events_window_min": [30, 60],
}


def sample_random(trial_id: int, rng: random.Random) -> dict:
    symbols = rng.choice(SPACE["symbols"])
    # max_positions: 単独は 1、2 銘柄は 1〜2、3 銘柄は 1〜3
    max_positions = rng.choice(list(range(1, len(symbols) + 1)))

    while True:
        ma_s = rng.choice(SPACE["ma_short"])
        ma_l = rng.choice(SPACE["ma_long"])
        if ma_l > ma_s:
            break

    use_ndx = rng.choice(SPACE["use_ndx"])
    ndx_s = rng.choice(SPACE["ndx_ma_short"]) if use_ndx else None
    ndx_l = rng.choice(SPACE["ndx_ma_long"]) if use_ndx else None
    if use_ndx and ndx_l <= ndx_s:
        ndx_l = 20 if ndx_s < 20 else 50

    use_spx = rng.choice(SPACE["use_spx"])
    spx_s = rng.choice(SPACE["spx_ma_short"]) if use_spx else None
    spx_l = rng.choice(SPACE["spx_ma_long"]) if use_spx else None
    if use_spx and spx_l <= spx_s:
        spx_l = 20 if spx_s < 20 else 50

    use_vix = rng.choice(SPACE["use_vix"])
    vix_max = rng.choice(SPACE["vix_max"]) if use_vix else None

    use_us_hours = rng.choice(SPACE["use_us_hours"])
    use_events = rng.choice(SPACE["use_events"])
    events_w = rng.choice(SPACE["events_window_min"]) if use_events else None

    return {
        "trial_id": trial_id, "stage": 1,
        "symbols": symbols, "max_positions": max_positions,
        "buy_trend": rng.choice(SPACE["buy_trend"]),
        "ma_short": ma_s, "ma_long": ma_l,
        "tp_pct": rng.choice(SPACE["tp_pct"]),
        "sl_pct": rng.choice(SPACE["sl_pct"]),
        "max_hold_bars": rng.choice(SPACE["max_hold_bars"]),
        "trail_pct": rng.choice(SPACE["trail_pct"]),
        "cooldown_min": rng.choice(SPACE["cooldown_min"]),
        "use_ndx": use_ndx, "ndx_ma_short": ndx_s, "ndx_ma_long": ndx_l,
        "use_spx": use_spx, "spx_ma_short": spx_s, "spx_ma_long": spx_l,
        "use_vix": use_vix, "vix_max": vix_max,
        "use_us_hours": use_us_hours,
        "use_events": use_events, "events_window_min": events_w,
    }


def _clip(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _clip_f(v: float, lo: float, hi: float) -> float:
    return round(max(lo, min(hi, v)), 1)


def sample_neighbor(base: dict, trial_id: int, rng: random.Random) -> dict:
    # int: ±2, tp/sl: ±1.0 (step 0.5)
    def jitter_int(name, lo, hi):
        return _clip(base[name] + rng.randint(-2, 2), lo, hi)

    ma_s = jitter_int("ma_short", 3, 10)
    ma_l = jitter_int("ma_long", 15, 50)
    if ma_l <= ma_s:
        ma_l = ma_s + 5
        if ma_l > 50:
            ma_s = max(3, ma_l - 5)

    buy_trend = jitter_int("buy_trend", 3, 15)
    tp = _clip_f(base["tp_pct"] + rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0]), 1.5, 8.0)
    sl = _clip_f(base["sl_pct"] + rng.choice([-1.0, -0.5, 0.0, 0.5, 1.0]), -4.0, -1.0)

    # Exit 拡張: ±1 step 程度で変動
    def jitter_choice(val, options):
        if rng.random() < 0.3:
            return rng.choice(options)
        return val
    max_hold_bars = jitter_choice(base.get("max_hold_bars", 0), SPACE["max_hold_bars"])
    trail_pct = jitter_choice(base.get("trail_pct", 0.0), SPACE["trail_pct"])
    cooldown_min = jitter_choice(base.get("cooldown_min", 0), SPACE["cooldown_min"])

    # フィルター: 大筋 base を踏襲、30% で ma を入れ替え、10% で on/off を反転
    def neighbor_filter(on_key: str, params_keys: list[str], opts: dict):
        on_now = base.get(on_key, False)
        if rng.random() < 0.1:
            on_now = not on_now
        vals = {}
        for k in params_keys:
            v = base.get(k)
            if on_now and (v is None or rng.random() < 0.3):
                v = rng.choice(opts[k])
            vals[k] = v if on_now else None
        return on_now, vals

    use_ndx, ndx_v = neighbor_filter(
        "use_ndx", ["ndx_ma_short", "ndx_ma_long"],
        {"ndx_ma_short": SPACE["ndx_ma_short"], "ndx_ma_long": SPACE["ndx_ma_long"]},
    )
    if use_ndx and ndx_v["ndx_ma_long"] and ndx_v["ndx_ma_short"] and \
            ndx_v["ndx_ma_long"] <= ndx_v["ndx_ma_short"]:
        ndx_v["ndx_ma_long"] = 20 if ndx_v["ndx_ma_short"] < 20 else 50

    use_spx, spx_v = neighbor_filter(
        "use_spx", ["spx_ma_short", "spx_ma_long"],
        {"spx_ma_short": SPACE["spx_ma_short"], "spx_ma_long": SPACE["spx_ma_long"]},
    )
    if use_spx and spx_v["spx_ma_long"] and spx_v["spx_ma_short"] and \
            spx_v["spx_ma_long"] <= spx_v["spx_ma_short"]:
        spx_v["spx_ma_long"] = 20 if spx_v["spx_ma_short"] < 20 else 50

    use_vix = base.get("use_vix", False)
    if rng.random() < 0.1:
        use_vix = not use_vix
    vix_max = None
    if use_vix:
        vix_max = base.get("vix_max")
        if vix_max is None or rng.random() < 0.3:
            vix_max = rng.choice(SPACE["vix_max"])

    use_us_hours = base.get("use_us_hours", False)
    if rng.random() < 0.15:
        use_us_hours = not use_us_hours

    use_events = base.get("use_events", False)
    if rng.random() < 0.15:
        use_events = not use_events
    events_w = None
    if use_events:
        events_w = base.get("events_window_min") or rng.choice(SPACE["events_window_min"])

    # symbols / max_positions は基本 base を踏襲、15% で変更
    symbols = list(base.get("symbols") or ["BTC"])
    if rng.random() < 0.15:
        symbols = rng.choice(SPACE["symbols"])
    max_positions = base.get("max_positions", 1)
    if rng.random() < 0.2 or max_positions > len(symbols):
        max_positions = rng.choice(list(range(1, len(symbols) + 1)))

    return {
        "trial_id": trial_id, "stage": 2,
        "symbols": symbols, "max_positions": max_positions,
        "buy_trend": buy_trend,
        "ma_short": ma_s, "ma_long": ma_l,
        "tp_pct": tp, "sl_pct": sl,
        "max_hold_bars": max_hold_bars,
        "trail_pct": trail_pct, "cooldown_min": cooldown_min,
        "use_ndx": use_ndx,
        "ndx_ma_short": ndx_v["ndx_ma_short"], "ndx_ma_long": ndx_v["ndx_ma_long"],
        "use_spx": use_spx,
        "spx_ma_short": spx_v["spx_ma_short"], "spx_ma_long": spx_v["spx_ma_long"],
        "use_vix": use_vix, "vix_max": vix_max,
        "use_us_hours": use_us_hours,
        "use_events": use_events, "events_window_min": events_w,
    }


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------
def _row_to_csv(row: dict) -> list:
    return [row.get(c, "") for c in CSV_COLUMNS]


def write_summary_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_COLUMNS)
        for row in rows:
            w.writerow(_row_to_csv(row))


# ---------------------------------------------------------------------------
# Top20 詳細再実行
# ---------------------------------------------------------------------------
def rerun_with_trades(candles, ts_list, ndx_bars, spx_bars, vix_bars,
                      events, param: dict, sym_data: dict | None = None) -> dict:
    import optimize_common as oc
    oc._init_worker(candles, ts_list, ndx_bars, spx_bars, vix_bars, events,
                    sym_data=sym_data)

    filters = oc._build_filters(param)
    out = {}
    symbols = param.get("symbols") or ["BTC"]
    max_pos = int(param.get("max_positions", 1))
    for pname in PERIOD_NAMES:
        res = oc._run_bt_multi_cached(
            symbols, oc.PERIODS_4[pname],
            buy_trend=param["buy_trend"],
            ma_short=param["ma_short"], ma_long=param["ma_long"],
            tp_pct=param["tp_pct"], sl_pct=param["sl_pct"],
            filters=filters,
            max_hold_bars=param.get("max_hold_bars", 0),
            trail_pct=param.get("trail_pct", 0.0),
            cooldown_min=param.get("cooldown_min", 0),
            max_positions=max_pos,
        )
        from backtest_v1_tf import analyze, monthly_breakdown
        out[pname] = {
            "stats": analyze(res, oc.PERIODS_4[pname]),
            "monthly": monthly_breakdown(res),
            "trades": res["trades"],
            "raw_signals": res.get("raw_signals", 0),
            "filter_skips": res.get("filter_skips", {}),
        }
    return out


# ---------------------------------------------------------------------------
# REPORT.md 生成
# ---------------------------------------------------------------------------
def format_report(top_rows: list[dict], args, wall_sec: float,
                  n_total: int, n_ok: int, n_err: int) -> str:
    lines = []
    lines.append(f"# Local Optimize レポート ({n_total} trial)")
    lines.append("")
    lines.append(f"- 実行時間: {wall_sec:.1f} sec ({wall_sec/60.0:.1f} min)")
    lines.append(f"- 成功/エラー: {n_ok}/{n_err}")
    lines.append(f"- workers: {args.workers}")
    lines.append(f"- seed: {args.seed}")
    lines.append(f"- composite 重み: α={args.alpha}, β={args.beta}, γ={args.gamma}")
    lines.append(f"- Stage1 trials: {int(args.trials * args.stage1_frac)}, "
                 f"Stage2 K={args.stage2_k} × neighbors={args.stage2_neighbors}")
    lines.append("")
    lines.append("## Top 20 候補")
    lines.append("")
    lines.append(
        "| rank | trial_id | stage | composite | symbols (mp) | core params | exit extras | "
        "filters | PF_te | PF_tr | PF_val | PF_fin | DDmax | trades(te/tr/v/f) |"
    )
    lines.append(
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    for i, r in enumerate(top_rows[:20], 1):
        syms = r.get("symbols") or "BTC"
        mp = r.get("max_positions") or 1
        sym_str = f"{syms} ({mp})"
        core = (f"t{r['buy_trend']} {r['ma_short']}/{r['ma_long']} "
                f"tp{r['tp_pct']} sl{r['sl_pct']}")
        exit_extras_parts = []
        if r.get('max_hold_bars', 0):
            exit_extras_parts.append(f"mh{r['max_hold_bars']}")
        if r.get('trail_pct', 0.0) > 0:
            exit_extras_parts.append(f"tr{r['trail_pct']}")
        if r.get('cooldown_min', 0):
            exit_extras_parts.append(f"cd{r['cooldown_min']}")
        exit_extras = " ".join(exit_extras_parts) or "-"

        fparts = []
        if int(r.get('use_ndx', 0)):
            fparts.append(f"ndx{r.get('ndx_ma_short', 0)}/{r.get('ndx_ma_long', 0)}")
        if int(r.get('use_spx', 0)):
            fparts.append(f"spx{r.get('spx_ma_short', 0)}/{r.get('spx_ma_long', 0)}")
        if int(r.get('use_vix', 0)):
            fparts.append(f"vix<{r.get('vix_max', 0)}")
        if int(r.get('use_us_hours', 0)):
            fparts.append("ushr")
        if int(r.get('use_events', 0)):
            fparts.append(f"ev{r.get('events_window_min', 0)}")
        filters_str = ",".join(fparts) or "-"

        lines.append(
            f"| {i} | {r['trial_id']} | {r.get('stage', 1)} | "
            f"{r['composite']:+.3f} | {sym_str} | {core} | {exit_extras} | {filters_str} | "
            f"{r['train_extra_pf']} | {r['train_pf']} | "
            f"{r['val_pf']} | {r['final_pf']} | "
            f"{r['dd_max']:.2f}% | "
            f"{r['train_extra_trades']}/{r['train_trades']}/"
            f"{r['val_trades']}/{r['final_trades']} |"
        )
    lines.append("")
    lines.append("## 注記")
    lines.append("- `composite = mean(PF) - α·std(PF) - β·maxDD/100 - γ·|PF_tr - PF_val| - 1.0·trade_guard`")
    lines.append("- PF は 999.99 が事実上の ∞ (全勝) を表す")
    lines.append("- `trade_guard` は trades<10 の期間数。値が大きいほど参考程度の結果")
    lines.append("- 詳細 trades は `top20/trial_XX_id{N}.json` 参照")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--trials", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--stage1-frac", type=float, default=0.8)
    ap.add_argument("--stage2-k", type=int, default=20)
    ap.add_argument("--stage2-neighbors", type=int, default=10)
    ap.add_argument("--skip-stage2", action="store_true")
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--beta", type=float, default=0.4)
    ap.add_argument("--gamma", type=float, default=0.3)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--market", default="./data/market")
    ap.add_argument("--out", default="./data/backtest/optimize_local")
    ap.add_argument("--flush-every", type=int, default=100)
    ap.add_argument("--chunksize", type=int, default=4,
                    help="executor.map chunksize")
    ap.add_argument("--max-tasks-per-child", type=int, default=400,
                    help="worker 再起動までの trial 数 (メモリ再利用制限)")
    ap.add_argument("--resume", action="store_true",
                    help="既存 summary.csv を読み込み、残り分だけ実行")
    ap.add_argument("--batch-size", type=int, default=0,
                    help="1 batch の trial 数。0 で無効 (全 trial 一括)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    top_dir = out_dir / "top20"
    top_dir.mkdir(exist_ok=True)

    t_wall_start = time.time()
    print(f"[load] BTC/ETH candles from {args.data}", flush=True)
    btc_candles = load_symbol_candles(Path(args.data), "BTC_JPY")
    btc_ts = [c.ts for c in btc_candles]
    eth_candles = load_symbol_candles(Path(args.data), "ETH_JPY")
    eth_ts = [c.ts for c in eth_candles]
    xrp_candles = load_symbol_candles(Path(args.data), "XRP_JPY")
    xrp_ts = [c.ts for c in xrp_candles]
    print(f"[load] BTC {len(btc_candles)} ETH {len(eth_candles)} "
          f"XRP {len(xrp_candles)}", flush=True)

    # 後方互換エイリアス
    candles = btc_candles
    ts_list = btc_ts

    sym_data = {
        "BTC": (btc_candles, btc_ts),
        "ETH": (eth_candles, eth_ts),
        "XRP": (xrp_candles, xrp_ts),
    }

    ndx_bars = load_daily_csv(Path(args.market) / "NDX_d.csv")
    spx_bars = load_daily_csv(Path(args.market) / "SPX_d.csv")
    vix_bars = load_daily_csv(Path(args.market) / "VIX_d.csv")
    events = generate_events_calendar(2022, 2026)
    print(f"[load] NDX={len(ndx_bars)} SPX={len(spx_bars)} VIX={len(vix_bars)} "
          f"events={len(events)}", flush=True)

    # ------------------------------------------------------------------
    # Resume: 既存 summary.csv を読む
    # ------------------------------------------------------------------
    summary_csv = out_dir / "summary.csv"
    existing_rows: list[dict] = []
    done_keys: set[str] = set()
    max_existing_tid = -1
    if args.resume and summary_csv.exists():
        with summary_csv.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                # 旧 CSV (symbols/max_positions なし) のデフォルト補填
                if "symbols" not in r or not r.get("symbols"):
                    r["symbols"] = "BTC"
                if "max_positions" not in r or not r.get("max_positions"):
                    r["max_positions"] = "1"
                existing_rows.append(r)
                syms_raw = r.get("symbols") or "BTC"
                syms = syms_raw.split("+") if syms_raw else ["BTC"]
                k = param_key({
                    "symbols": syms,
                    "max_positions": int(float(r.get("max_positions", 1) or 1)),
                    "buy_trend": int(float(r.get("buy_trend", 0))),
                    "ma_short": int(float(r.get("ma_short", 0))),
                    "ma_long": int(float(r.get("ma_long", 0))),
                    "tp_pct": float(r.get("tp_pct", 0)),
                    "sl_pct": float(r.get("sl_pct", 0)),
                    "max_hold_bars": int(float(r.get("max_hold_bars", 0))),
                    "trail_pct": float(r.get("trail_pct", 0)),
                    "cooldown_min": int(float(r.get("cooldown_min", 0))),
                    "use_ndx": bool(int(float(r.get("use_ndx", 0)))),
                    "ndx_ma_short": int(float(r.get("ndx_ma_short", 0))) or None,
                    "ndx_ma_long": int(float(r.get("ndx_ma_long", 0))) or None,
                    "use_spx": bool(int(float(r.get("use_spx", 0)))),
                    "spx_ma_short": int(float(r.get("spx_ma_short", 0))) or None,
                    "spx_ma_long": int(float(r.get("spx_ma_long", 0))) or None,
                    "use_vix": bool(int(float(r.get("use_vix", 0)))),
                    "vix_max": float(r.get("vix_max", 0)) or None,
                    "use_us_hours": bool(int(float(r.get("use_us_hours", 0)))),
                    "use_events": bool(int(float(r.get("use_events", 0)))),
                    "events_window_min": int(float(r.get("events_window_min", 0))) or None,
                })
                done_keys.add(k)
                try:
                    max_existing_tid = max(max_existing_tid,
                                           int(float(r.get("trial_id", -1))))
                except (ValueError, TypeError):
                    pass
        print(f"[resume] loaded {len(existing_rows)} existing rows, "
              f"max_tid={max_existing_tid}", flush=True)

    # サンプラー: stage1 生成
    rng = random.Random(args.seed)
    n_stage1 = int(args.trials * args.stage1_frac) if not args.skip_stage2 else args.trials
    n_stage2_plan = args.trials - n_stage1 if not args.skip_stage2 else 0

    stage1_params: list[dict] = []
    seen_keys: set[str] = set(done_keys)
    tid = max(0, max_existing_tid + 1)
    max_attempts = n_stage1 * 10
    attempts = 0
    # resume 時は「これから生成する追加分」 = n_stage1 - (既存 stage1)
    existing_stage1_count = sum(
        1 for r in existing_rows
        if int(float(r.get("stage", 1))) == 1 and r.get("status") == "ok"
    )
    n_stage1_remaining = max(0, n_stage1 - existing_stage1_count)
    while len(stage1_params) < n_stage1_remaining and attempts < max_attempts:
        p = sample_random(tid, rng)
        k = param_key(p)
        if k not in seen_keys:
            seen_keys.add(k)
            stage1_params.append(p)
            tid += 1
        attempts += 1
    print(f"[sampler] stage1 new: {len(stage1_params)} (existing stage1 ok: "
          f"{existing_stage1_count}, target: {n_stage1})", flush=True)

    # ProcessPool で Stage1 実行
    all_rows: list[dict] = list(existing_rows)  # resume 時は既存を含める
    # CSV mode: resume なら append、新規なら truncate
    if args.resume and summary_csv.exists():
        print(f"[csv] append mode to existing summary.csv", flush=True)
    else:
        with summary_csv.open("w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerow(CSV_COLUMNS)

    def flush_rows(rows_buf: list[dict]) -> None:
        with summary_csv.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            for r in rows_buf:
                w.writerow(_row_to_csv(r))

    def run_one_pool(params_chunk: list[dict], stage_label: str,
                     offset: int, total: int) -> list[dict]:
        rows: list[dict] = []
        buf: list[dict] = []
        t_start = time.time()
        fn = partial(_run_trial, alpha=args.alpha, beta=args.beta, gamma=args.gamma)
        report_every = max(10, total // 20)
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=_init_worker,
            initargs=(candles, ts_list, ndx_bars, spx_bars, vix_bars, events, sym_data),
            max_tasks_per_child=args.max_tasks_per_child,
        ) as ex:
            done_local = 0
            for row in ex.map(fn, params_chunk, chunksize=args.chunksize):
                rows.append(row)
                buf.append(row)
                done_local += 1
                if len(buf) >= args.flush_every:
                    flush_rows(buf)
                    buf = []
                global_done = offset + done_local
                if done_local % report_every == 0 or global_done == total:
                    elapsed = time.time() - t_start
                    rate = done_local / elapsed if elapsed > 0 else 0.0
                    eta = (total - global_done) / rate if rate > 0 else 0.0
                    print(f"  [{stage_label}] {global_done}/{total}  "
                          f"rate={rate:.2f}/s  eta={eta:.0f}s", flush=True)
            if buf:
                flush_rows(buf)
        return rows

    def run_batch(params: list[dict], stage_label: str) -> list[dict]:
        n = len(params)
        print(f"[run] {stage_label}: {n} trials × {args.workers} workers",
              flush=True)
        t_start = time.time()
        rows: list[dict] = []
        batch_size = args.batch_size if args.batch_size > 0 else n
        for i in range(0, n, batch_size):
            chunk = params[i:i + batch_size]
            if args.batch_size > 0:
                print(f"  [batch] {i}-{i+len(chunk)} / {n}", flush=True)
            sub = run_one_pool(chunk, stage_label, offset=i, total=n)
            rows.extend(sub)
        print(f"[run] {stage_label} done in {time.time()-t_start:.1f}s", flush=True)
        return rows

    if stage1_params:
        stage1_rows = run_batch(stage1_params, "stage1")
        all_rows.extend(stage1_rows)
    else:
        print("[sampler] stage1 skipped (all existing)", flush=True)

    # Stage 2: 近傍探索 (resume 時は既存 + 新 stage1 合算から top-K 選出)
    existing_stage2_count = sum(
        1 for r in existing_rows
        if int(float(r.get("stage", 1))) == 2 and r.get("status") == "ok"
    )
    n_stage2_remaining = max(0, n_stage2_plan - existing_stage2_count)
    if not args.skip_stage2 and n_stage2_remaining > 0:
        # 既存 + 新 stage1 すべてから composite 高い順
        def _comp(r):
            try:
                return float(r["composite"])
            except (KeyError, ValueError, TypeError):
                return -9999.0
        ok_rows = [r for r in all_rows if r.get("status") == "ok"]
        ok_rows.sort(key=_comp, reverse=True)
        seeds = ok_rows[:args.stage2_k]
        print(f"[sampler] stage2: top {len(seeds)} seeds × {args.stage2_neighbors}",
              flush=True)

        # existing_rows は str 混入、新 rows は数値型。どちらでも動く変換子。
        def _i(v, d=0):
            try: return int(float(v))
            except (ValueError, TypeError): return d
        def _f(v, d=0.0):
            try: return float(v)
            except (ValueError, TypeError): return d

        stage2_params: list[dict] = []
        for seed_row in seeds:
            syms_raw = seed_row.get("symbols") or "BTC"
            base = {
                "symbols": syms_raw.split("+") if syms_raw else ["BTC"],
                "max_positions": _i(seed_row.get("max_positions")) or 1,
                "buy_trend": _i(seed_row.get("buy_trend")),
                "ma_short": _i(seed_row.get("ma_short")),
                "ma_long": _i(seed_row.get("ma_long")),
                "tp_pct": _f(seed_row.get("tp_pct")),
                "sl_pct": _f(seed_row.get("sl_pct")),
                "max_hold_bars": _i(seed_row.get("max_hold_bars")),
                "trail_pct": _f(seed_row.get("trail_pct")),
                "cooldown_min": _i(seed_row.get("cooldown_min")),
                "use_ndx": bool(_i(seed_row.get("use_ndx"))),
                "ndx_ma_short": _i(seed_row.get("ndx_ma_short")) or None,
                "ndx_ma_long": _i(seed_row.get("ndx_ma_long")) or None,
                "use_spx": bool(_i(seed_row.get("use_spx"))),
                "spx_ma_short": _i(seed_row.get("spx_ma_short")) or None,
                "spx_ma_long": _i(seed_row.get("spx_ma_long")) or None,
                "use_vix": bool(_i(seed_row.get("use_vix"))),
                "vix_max": _f(seed_row.get("vix_max")) or None,
                "use_us_hours": bool(_i(seed_row.get("use_us_hours"))),
                "use_events": bool(_i(seed_row.get("use_events"))),
                "events_window_min": _i(seed_row.get("events_window_min")) or None,
            }
            for _ in range(args.stage2_neighbors):
                if len(stage2_params) >= n_stage2_remaining:
                    break
                nb = sample_neighbor(base, tid, rng)
                k = param_key(nb)
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                stage2_params.append(nb)
                tid += 1
            if len(stage2_params) >= n_stage2_remaining:
                break

        if stage2_params:
            stage2_rows = run_batch(stage2_params, "stage2")
            all_rows.extend(stage2_rows)

    # ソート & Top20 再実行 (existing_rows は str 混在なので型変換が必要)
    def _safe_comp(r):
        try:
            return float(r.get("composite", -9999))
        except (ValueError, TypeError):
            return -9999.0
    ok_all = [r for r in all_rows if r.get("status") == "ok"]
    ok_all.sort(key=_safe_comp, reverse=True)
    err_n = len(all_rows) - len(ok_all)

    # 正規化 (top 20 表示 & 再実行用)
    def _i(v, d=0):
        try: return int(float(v))
        except (ValueError, TypeError): return d
    def _f(v, d=0.0):
        try: return float(v)
        except (ValueError, TypeError): return d

    print(f"[top] Re-running top 20 for detailed trades...", flush=True)
    for rank, row in enumerate(ok_all[:20], 1):
        syms_raw = row.get("symbols") or "BTC"
        param = {
            "symbols": syms_raw.split("+") if syms_raw else ["BTC"],
            "max_positions": _i(row.get("max_positions")) or 1,
            "buy_trend": _i(row.get("buy_trend")),
            "ma_short": _i(row.get("ma_short")), "ma_long": _i(row.get("ma_long")),
            "tp_pct": _f(row.get("tp_pct")), "sl_pct": _f(row.get("sl_pct")),
            "max_hold_bars": _i(row.get("max_hold_bars")),
            "trail_pct": _f(row.get("trail_pct")),
            "cooldown_min": _i(row.get("cooldown_min")),
            "use_ndx": bool(_i(row.get("use_ndx"))),
            "ndx_ma_short": _i(row.get("ndx_ma_short")) or None,
            "ndx_ma_long": _i(row.get("ndx_ma_long")) or None,
            "use_spx": bool(_i(row.get("use_spx"))),
            "spx_ma_short": _i(row.get("spx_ma_short")) or None,
            "spx_ma_long": _i(row.get("spx_ma_long")) or None,
            "use_vix": bool(_i(row.get("use_vix"))),
            "vix_max": _f(row.get("vix_max")) or None,
            "use_us_hours": bool(_i(row.get("use_us_hours"))),
            "use_events": bool(_i(row.get("use_events"))),
            "events_window_min": _i(row.get("events_window_min")) or None,
        }
        try:
            detail = rerun_with_trades(candles, ts_list, ndx_bars, spx_bars,
                                       vix_bars, events, param, sym_data=sym_data)
            payload = {
                "rank": rank, "trial_id": row["trial_id"],
                "composite": row["composite"], "param": param,
                "summary_row": row, "per_period": detail,
            }
            path = top_dir / f"trial_{rank:02d}_id{row['trial_id']}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  rank {rank} re-run failed: {e}", flush=True)

    # REPORT (str 混入を正規化)
    def _norm_row(r):
        out = dict(r)
        for k in ("trial_id", "stage", "buy_trend", "ma_short", "ma_long",
                  "max_hold_bars", "cooldown_min",
                  "use_ndx", "ndx_ma_short", "ndx_ma_long",
                  "use_spx", "spx_ma_short", "spx_ma_long",
                  "use_vix", "use_us_hours", "use_events", "events_window_min",
                  "train_extra_trades", "train_trades", "val_trades",
                  "final_trades", "trade_guard"):
            if k in out:
                try: out[k] = int(float(out[k]))
                except (ValueError, TypeError): out[k] = 0
        for k in ("tp_pct", "sl_pct", "trail_pct", "vix_max",
                  "train_extra_pf", "train_pf", "val_pf", "final_pf",
                  "train_extra_dd", "train_dd", "val_dd", "final_dd",
                  "train_extra_net_pct", "train_net_pct", "val_net_pct",
                  "final_net_pct", "pf_mean", "pf_std", "dd_max",
                  "pf_spread", "composite", "wall_sec"):
            if k in out:
                try: out[k] = float(out[k])
                except (ValueError, TypeError): out[k] = 0.0
        return out
    wall = time.time() - t_wall_start
    report = format_report([_norm_row(r) for r in ok_all], args, wall,
                           len(all_rows), len(ok_all), err_n)
    (out_dir / "REPORT.md").write_text(report, encoding="utf-8")

    print(f"\n[save] {out_dir}/summary.csv  ({len(all_rows)} rows, {err_n} errors)",
          flush=True)
    print(f"[save] {top_dir}/  ({min(20, len(ok_all))} files)", flush=True)
    print(f"[save] {out_dir}/REPORT.md", flush=True)
    print(f"[wall] {wall:.1f}s ({wall/60:.1f} min)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
