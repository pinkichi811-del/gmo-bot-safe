#!/usr/bin/env python3
"""BTC 上昇場面の探索的分析。

目的: 「伸びる前の共通特徴」を距離感で把握する。予測モデルは後。

パイプライン:
  1. 全 bar で特徴量ベクトルを計算
  2. 複数定義で上昇イベント判定（rally / non-event）
  3. event vs non-event の feature 分布比較（mean / median / lift）
  4. ルールベースのパターン分類
  5. 代表事例の列挙
  6. markdown + JSON で保存
"""
from __future__ import annotations

import bisect
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_watcher import Candle  # noqa: E402


# ---------------------------------------------------------------------------
# データロード
# ---------------------------------------------------------------------------
def load_candles(data_root: Path) -> list[Candle]:
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
# 指標
# ---------------------------------------------------------------------------
def atr_at(candles: list[Candle], i: int, n: int = 14) -> float:
    if i < n:
        return 0.0
    trs = 0.0
    for k in range(i - n + 1, i + 1):
        c = candles[k]
        pc = candles[k - 1].close
        tr = max(c.high - c.low, abs(c.high - pc), abs(c.low - pc))
        trs += tr
    return trs / n


def sma_at(candles: list[Candle], i: int, n: int) -> float:
    if i < n - 1:
        return 0.0
    s = sum(candles[k].close for k in range(i - n + 1, i + 1))
    return s / n


def rsi_at(candles: list[Candle], i: int, period: int = 14) -> float:
    if i < period:
        return 50.0
    gains = 0.0
    losses = 0.0
    for k in range(i - period + 1, i + 1):
        diff = candles[k].close - candles[k - 1].close
        if diff > 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


def atr_percentile(atr_value: float, atr_history: list[float]) -> float:
    if not atr_history:
        return 50.0
    below = sum(1 for a in atr_history if a <= atr_value)
    return below / len(atr_history) * 100.0


def consecutive_bull(candles: list[Candle], i: int, max_look: int = 10) -> int:
    n = 0
    for k in range(i, max(-1, i - max_look), -1):
        if k < 0:
            break
        if candles[k].close > candles[k].open:
            n += 1
        else:
            break
    return n


# ---------------------------------------------------------------------------
# 特徴量ベクトル
# ---------------------------------------------------------------------------
MIN_HISTORY = 210  # MA200 + ATR 余裕


def compute_features(candles: list[Candle], i: int) -> dict[str, float]:
    c = candles[i]
    atr14 = atr_at(candles, i, 14)
    atr_hist = [atr_at(candles, k, 14) for k in range(i - 100, i)]  # last 100 bars
    atr_pct = atr_percentile(atr14, atr_hist)
    ma5 = sma_at(candles, i, 5)
    ma20 = sma_at(candles, i, 20)
    ma50 = sma_at(candles, i, 50)
    ma200 = sma_at(candles, i, 200)

    rsi14 = rsi_at(candles, i, 14)

    # 直近高値更新
    recent20 = candles[i - 20:i]
    prior_high20 = max(x.high for x in recent20)
    prior_low20 = min(x.low for x in recent20)
    high_broken = c.close > prior_high20

    recent40 = candles[i - 40:i]
    prior_high40 = max(x.high for x in recent40)
    high_broken_40 = c.close > prior_high40

    # レンジ幅（ATR 単位）
    range_width_atr = (prior_high20 - prior_low20) / atr14 if atr14 > 0 else 0.0

    # 持ち合い長さ（何本連続で range20 内か、近似）
    range_length = 0
    rh = prior_high20
    rl = prior_low20
    for k in range(i - 1, max(-1, i - 60), -1):
        if k < 0:
            break
        if rl <= candles[k].close <= rh:
            range_length += 1
        else:
            break

    # bar 性質
    bar_range = c.high - c.low
    body = abs(c.close - c.open)
    body_ratio = body / bar_range if bar_range > 0 else 0.0
    upper_wick = c.high - max(c.close, c.open)
    lower_wick = min(c.close, c.open) - c.low
    upper_wick_ratio = upper_wick / bar_range if bar_range > 0 else 0.0
    lower_wick_ratio = lower_wick / bar_range if bar_range > 0 else 0.0
    bar_size_atr = bar_range / atr14 if atr14 > 0 else 0.0
    bar_direction = 1.0 if c.close > c.open else (-1.0 if c.close < c.open else 0.0)

    # MA 関連
    ma5_above_ma20 = 1.0 if ma5 > ma20 else 0.0
    ma20_above_ma50 = 1.0 if ma20 > ma50 else 0.0
    ma50_above_ma200 = 1.0 if ma50 > ma200 else 0.0
    # slope (5 bar slope of MA20)
    ma20_5ago = sma_at(candles, i - 5, 20) if i - 5 >= 19 else ma20
    ma20_slope_norm = (ma20 - ma20_5ago) / c.close if c.close > 0 else 0.0
    ma50_10ago = sma_at(candles, i - 10, 50) if i - 10 >= 49 else ma50
    ma50_slope_norm = (ma50 - ma50_10ago) / c.close if c.close > 0 else 0.0

    # リターン
    ret_3 = (c.close / candles[i - 3].close - 1.0) if i >= 3 else 0.0
    ret_5 = (c.close / candles[i - 5].close - 1.0) if i >= 5 else 0.0
    ret_10 = (c.close / candles[i - 10].close - 1.0) if i >= 10 else 0.0

    consec = consecutive_bull(candles, i)

    # 出来高
    vol_avg20 = sum(x.volume for x in recent20) / 20.0
    vol_ratio = c.volume / vol_avg20 if vol_avg20 > 0 else 0.0
    vol_ratio_prev3 = 0.0
    if i >= 3 and vol_avg20 > 0:
        vol_ratio_prev3 = sum(candles[k].volume for k in range(i - 2, i + 1)) / 3.0 / vol_avg20

    # ATR 水準
    atr_pct_of_price = (atr14 / c.close * 100.0) if c.close > 0 else 0.0

    # 直近急騰
    max_bar_ret_last5 = 0.0
    for k in range(i - 4, i + 1):
        if k < 1:
            continue
        r = (candles[k].close / candles[k - 1].close - 1.0)
        if r > max_bar_ret_last5:
            max_bar_ret_last5 = r

    # レンジ上限からの距離
    dist_from_range_top_pct = (prior_high20 - c.close) / c.close * 100.0 if c.close > 0 else 0.0

    # 上位足トレンド: MA200 上に居るか
    above_ma200 = 1.0 if c.close > ma200 else 0.0

    return {
        "high_broken_20": 1.0 if high_broken else 0.0,
        "high_broken_40": 1.0 if high_broken_40 else 0.0,
        "range_width_atr": range_width_atr,
        "range_length_bars": float(range_length),
        "body_ratio": body_ratio,
        "upper_wick_ratio": upper_wick_ratio,
        "lower_wick_ratio": lower_wick_ratio,
        "bar_size_atr": bar_size_atr,
        "bar_direction": bar_direction,
        "ma5_above_ma20": ma5_above_ma20,
        "ma20_above_ma50": ma20_above_ma50,
        "ma50_above_ma200": ma50_above_ma200,
        "ma20_slope_norm": ma20_slope_norm,
        "ma50_slope_norm": ma50_slope_norm,
        "above_ma200": above_ma200,
        "ret_3bar": ret_3,
        "ret_5bar": ret_5,
        "ret_10bar": ret_10,
        "consec_bull_bars": float(consec),
        "rsi": rsi14,
        "atr_pct_of_price": atr_pct_of_price,
        "atr_percentile": atr_pct,
        "volume_ratio": vol_ratio,
        "volume_ratio_prev3": vol_ratio_prev3,
        "max_bar_ret_last5": max_bar_ret_last5,
        "dist_from_range_top_pct": dist_from_range_top_pct,
    }


# ---------------------------------------------------------------------------
# ラベル定義
# ---------------------------------------------------------------------------
def label_rally(
    candles: list[Candle], i: int, n_bars: int, up_atr: float,
    down_atr_before: float | None = None,
) -> int | None:
    """i 時点から、次の n_bars 以内に +up_atr*ATR に到達するか。"""
    c = candles[i]
    atr14 = atr_at(candles, i, 14)
    if atr14 <= 0 or i + n_bars >= len(candles):
        return None
    up_thr = c.close + up_atr * atr14
    dn_thr = c.close - (down_atr_before or 0.0) * atr14
    for k in range(i + 1, i + 1 + n_bars):
        ck = candles[k]
        hit_up = ck.high >= up_thr
        hit_dn = (down_atr_before is not None) and (ck.low <= dn_thr)
        if hit_up and hit_dn:
            return 0 if down_atr_before is not None else 1
        if hit_up:
            return 1
        if hit_dn:
            return 0
    return 0 if down_atr_before is not None else 0


def label_drop(
    candles: list[Candle], i: int, n_bars: int, down_atr: float,
    up_atr_before: float,
) -> int | None:
    """鏡像: 下落 event。upside より先に -down_atr*ATR 到達で 1。"""
    c = candles[i]
    atr14 = atr_at(candles, i, 14)
    if atr14 <= 0 or i + n_bars >= len(candles):
        return None
    dn_thr = c.close - down_atr * atr14
    up_thr = c.close + up_atr_before * atr14
    for k in range(i + 1, i + 1 + n_bars):
        ck = candles[k]
        hit_up = ck.high >= up_thr
        hit_dn = ck.low <= dn_thr
        if hit_up and hit_dn:
            return 0  # up が先とみなす（保守的）
        if hit_dn:
            return 1
        if hit_up:
            return 0
    return 0


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------
def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    k = max(0, min(n - 1, int(p / 100.0 * (n - 1))))
    return sorted_values[k]


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) / 2.0
    return s[n // 2]


def compare_distributions(
    events: list[dict], non_events: list[dict], feature_names: list[str],
) -> list[dict[str, Any]]:
    """各 feature について event vs non-event の分布比較と効果サイズ。"""
    rows = []
    for name in feature_names:
        ev_vals = [f[name] for f in events]
        ne_vals = [f[name] for f in non_events]
        ev_mean = mean(ev_vals)
        ne_mean = mean(ne_vals)
        # pooled std で効果サイズ (Cohen's d)
        ev_var = sum((v - ev_mean) ** 2 for v in ev_vals) / len(ev_vals) if ev_vals else 0
        ne_var = sum((v - ne_mean) ** 2 for v in ne_vals) / len(ne_vals) if ne_vals else 0
        pooled = math.sqrt((ev_var + ne_var) / 2.0) if (ev_var + ne_var) > 0 else 0
        cohen_d = (ev_mean - ne_mean) / pooled if pooled > 0 else 0.0

        # 分位
        ev_s = sorted(ev_vals)
        ne_s = sorted(ne_vals)
        rows.append({
            "feature": name,
            "event_mean": ev_mean,
            "event_median": percentile(ev_s, 50),
            "event_p25": percentile(ev_s, 25),
            "event_p75": percentile(ev_s, 75),
            "nonevent_mean": ne_mean,
            "nonevent_median": percentile(ne_s, 50),
            "cohen_d": cohen_d,
            "abs_d": abs(cohen_d),
            "lift_mean": (ev_mean / ne_mean) if ne_mean not in (0, -0) else float("inf"),
        })
    rows.sort(key=lambda r: r["abs_d"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# 代表パターン分類（ルールベース）
# ---------------------------------------------------------------------------
def classify_pattern(f: dict[str, float]) -> str:
    """event の特徴ベクトルを 4 type に振り分ける。重複可能だが 1 type を割り当てる。"""
    # 1. 持ち合い上抜け型: high_broken + range_length 長い + atr_percentile 低め
    if f["high_broken_20"] == 1.0 and f["range_length_bars"] >= 10 and f["atr_percentile"] < 60:
        return "breakout_from_range"
    # 2. 出来高急増ブレイク型: volume_ratio 高 + high_broken_20
    if f["high_broken_20"] == 1.0 and f["volume_ratio"] >= 2.0:
        return "volume_surge_breakout"
    # 3. 押し目再加速型: ma5_above_ma20=1 + ret_5bar < 0 もしくは 直近 pullback + ret_3bar > 0
    if f["ma5_above_ma20"] == 1.0 and f["ma20_slope_norm"] > 0 and f["ret_5bar"] < 0.003 and f["ret_3bar"] > 0.001:
        return "pullback_resume"
    # 4. 低ボラ圧縮後の拡大型: atr_percentile 低 + bar_size_atr 大
    if f["atr_percentile"] < 40 and f["bar_size_atr"] >= 1.3:
        return "squeeze_expansion"
    # 5. 強いモメンタム継続: ret_5bar 正 + consec_bull >= 2 + RSI 50-70
    if f["ret_5bar"] > 0.003 and f["consec_bull_bars"] >= 2 and 50 <= f["rsi"] <= 70:
        return "momentum_continuation"
    return "other"


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
RALLY_DEFS = [
    {"name": "N4_U0.8",  "n_bars": 4,  "up_atr": 0.8,  "down_atr_before": None},
    {"name": "N8_U1.0",  "n_bars": 8,  "up_atr": 1.0,  "down_atr_before": None},
    {"name": "N12_U1.5", "n_bars": 12, "up_atr": 1.5,  "down_atr_before": None},
    {"name": "N8_U1.0_D0.5", "n_bars": 8, "up_atr": 1.0, "down_atr_before": 0.5},
    {"name": "N12_U1.5_D0.7", "n_bars": 12, "up_atr": 1.5, "down_atr_before": 0.7},
    # より選別的な定義: 強い上昇（本当に伸びる場面）
    {"name": "N12_U2.0_D1.0", "n_bars": 12, "up_atr": 2.0, "down_atr_before": 1.0},
    {"name": "N20_U2.5_D1.0", "n_bars": 20, "up_atr": 2.5, "down_atr_before": 1.0},
    {"name": "N24_U3.0_D1.5", "n_bars": 24, "up_atr": 3.0, "down_atr_before": 1.5},
]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--out", default="./data/analysis")
    ap.add_argument("--start", default="2024-01-01T00:00:00Z")
    ap.add_argument("--end",   default="2026-03-31T23:45:00Z")
    # 特徴量計算は重いので間引く
    ap.add_argument("--stride", type=int, default=3,
                    help="N 本おきに features を取る (default 3 = 15min 毎)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("[load] candles", flush=True)
    candles = load_candles(Path(args.data))
    ts_list = [c.ts for c in candles]
    print(f"[load] {len(candles)} candles", flush=True)

    start_ts = datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp()
    end_ts = datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp()
    i_start = max(MIN_HISTORY, bisect.bisect_left(ts_list, start_ts))
    i_end = bisect.bisect_right(ts_list, end_ts)
    print(f"[range] i={i_start}..{i_end}  stride={args.stride}", flush=True)

    # 全 bar で features とラベル辞書を作る
    print("[compute] features + labels", flush=True)
    import time
    t0 = time.time()
    feat_rows: list[dict[str, float]] = []
    labels: dict[str, list[int | None]] = {d["name"]: [] for d in RALLY_DEFS}
    indices: list[int] = []

    for i in range(i_start, i_end, args.stride):
        if i + 20 >= len(candles):
            break
        indices.append(i)
        feat_rows.append(compute_features(candles, i))
        for d in RALLY_DEFS:
            labels[d["name"]].append(label_rally(
                candles, i, d["n_bars"], d["up_atr"],
                d["down_atr_before"],
            ))
        if len(feat_rows) % 5000 == 0:
            print(f"  processed {len(feat_rows)} / est "
                  f"{(i_end - i_start)//args.stride}  "
                  f"elapsed={time.time()-t0:.1f}s", flush=True)

    print(f"[compute] {len(feat_rows)} rows in {time.time()-t0:.1f}s", flush=True)

    # ラベル分布
    print("\n[events] 各定義の件数:")
    event_counts = {}
    for name, labs in labels.items():
        total = sum(1 for x in labs if x is not None)
        pos = sum(1 for x in labs if x == 1)
        rate = pos / total * 100 if total > 0 else 0
        event_counts[name] = {"pos": pos, "total": total, "rate_pct": rate}
        print(f"  {name:<20}  events={pos:6d}/{total:6d}  rate={rate:5.2f}%")

    # メイン分析: 選別性重視で N20_U2.5_D1.0（強い上昇のみ）を使う
    main_def = "N20_U2.5_D1.0"
    print(f"\n[main] 分析用定義 = {main_def}")

    feature_names = list(feat_rows[0].keys())
    events = [feat_rows[k] for k, l in enumerate(labels[main_def]) if l == 1]
    nonevents_all = [feat_rows[k] for k, l in enumerate(labels[main_def]) if l == 0]
    print(f"[main] events={len(events)} non_events={len(nonevents_all)}")

    # 下落 event (鏡像比較用)
    print("[drop] computing drop labels (N20 -2.5 ATR before +1.0 ATR)")
    drop_labels = [
        label_drop(candles, idx, n_bars=20, down_atr=2.5, up_atr_before=1.0)
        for idx in indices
    ]
    drops = [feat_rows[k] for k, l in enumerate(drop_labels) if l == 1]
    print(f"[drop] drop_events={len(drops)}")

    # non-event をランダムに event と同数抜き取り
    random.seed(42)
    sample_size = min(len(events), len(nonevents_all))
    nonevents = random.sample(nonevents_all, sample_size)

    # 分布比較
    print("\n[compare] event vs non-event feature distribution")
    rows = compare_distributions(events, nonevents, feature_names)

    # rally vs drop の比較（鏡像対比）
    print("\n[compare] rally vs drop feature distribution")
    drops_sample = random.sample(drops, min(len(drops), len(events))) if drops else []
    rows_rd = compare_distributions(events, drops_sample, feature_names) if drops_sample else []

    # パターン分類（event のみ）
    patterns = Counter()
    pattern_examples: dict[str, list[int]] = defaultdict(list)
    for k, lab in enumerate(labels[main_def]):
        if lab != 1:
            continue
        p = classify_pattern(feat_rows[k])
        patterns[p] += 1
        if len(pattern_examples[p]) < 5:
            pattern_examples[p].append(indices[k])

    print("\n[patterns] 上昇 event の内訳:")
    total_ev = sum(patterns.values())
    for p, c in patterns.most_common():
        print(f"  {p:<25}  {c:>6d}  ({c/total_ev*100:5.1f}%)")

    # 代表事例
    print("\n[examples] 代表事例:")
    for p, idxs in pattern_examples.items():
        print(f"  -- {p} --")
        for idx in idxs[:3]:
            ts = datetime.fromtimestamp(candles[idx].ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            f = feat_rows[indices.index(idx)] if idx in indices else compute_features(candles, idx)
            print(f"    {ts} UTC  close={candles[idx].close:>10.0f}  "
                  f"atr%ile={f['atr_percentile']:5.1f}  "
                  f"range_len={f['range_length_bars']:3.0f}  "
                  f"vol_ratio={f['volume_ratio']:.2f}  "
                  f"rsi={f['rsi']:.0f}  "
                  f"high_broken={int(f['high_broken_20'])}")

    # 保存
    summary = {
        "event_counts": event_counts,
        "main_def": main_def,
        "main_events": len(events),
        "main_nonevents": sample_size,
        "drop_events": len(drops),
        "feature_comparison_top20": rows[:20],
        "feature_comparison_rally_vs_drop_top20": rows_rd[:20] if rows_rd else [],
        "feature_comparison_all": rows,
        "patterns": dict(patterns),
        "pattern_examples": {
            p: [datetime.fromtimestamp(candles[idx].ts, tz=timezone.utc).isoformat()
                for idx in idxs]
            for p, idxs in pattern_examples.items()
        },
    }
    (out_dir / "rally_analysis.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    # markdown レポート
    md = [f"# BTC 上昇場面の探索的分析\n"]
    md.append(f"- 期間: {args.start} .. {args.end}")
    md.append(f"- 5分足 {len(candles)} 本、stride={args.stride}（分析対象 {len(feat_rows)} 点）\n")
    md.append("## 上昇イベント定義ごとの件数\n")
    md.append("| 定義 | events | total | rate |")
    md.append("|---|---|---|---|")
    for name, d in event_counts.items():
        md.append(f"| {name} | {d['pos']} | {d['total']} | {d['rate_pct']:.2f}% |")
    md.append("")
    md.append(f"## 分析用定義: `{main_def}`  (events={len(events)}, nonevents={sample_size})\n")
    md.append("## 特徴量ランキング: rally vs non-event (|Cohen's d| 降順 top 20)\n")
    md.append("| 特徴量 | rally mean | non-event mean | rally median | cohen's d |")
    md.append("|---|---|---|---|---|")
    for r in rows[:20]:
        md.append(
            f"| {r['feature']} | {r['event_mean']:.3f} | "
            f"{r['nonevent_mean']:.3f} | {r['event_median']:.3f} | "
            f"{r['cohen_d']:+.3f} |"
        )
    md.append("")
    if rows_rd:
        md.append(f"## 特徴量ランキング: rally vs drop (|Cohen's d| 降順 top 20)  rally={len(events)} drop={len(drops_sample)}\n")
        md.append("| 特徴量 | rally mean | drop mean | rally median | cohen's d |")
        md.append("|---|---|---|---|---|")
        for r in rows_rd[:20]:
            md.append(
                f"| {r['feature']} | {r['event_mean']:.3f} | "
                f"{r['nonevent_mean']:.3f} | {r['event_median']:.3f} | "
                f"{r['cohen_d']:+.3f} |"
            )
        md.append("")
    md.append("## 上昇 event のパターン内訳\n")
    md.append("| パターン | 件数 | 割合 |")
    md.append("|---|---|---|")
    for p, c in patterns.most_common():
        md.append(f"| {p} | {c} | {c/total_ev*100:.1f}% |")
    md.append("")
    md.append("## 代表事例\n")
    for p, idxs in pattern_examples.items():
        md.append(f"### {p}")
        md.append("| timestamp | close | atr%ile | range_len | vol_ratio | rsi | high_broken |")
        md.append("|---|---|---|---|---|---|---|")
        for idx in idxs[:5]:
            ts = datetime.fromtimestamp(candles[idx].ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            fi = feat_rows[indices.index(idx)] if idx in indices else compute_features(candles, idx)
            md.append(
                f"| {ts} | {candles[idx].close:.0f} | {fi['atr_percentile']:.1f} | "
                f"{fi['range_length_bars']:.0f} | {fi['volume_ratio']:.2f} | "
                f"{fi['rsi']:.0f} | {int(fi['high_broken_20'])} |"
            )
        md.append("")

    (out_dir / "rally_analysis.md").write_text("\n".join(md), encoding="utf-8")
    print(f"\n[save] {out_dir}/rally_analysis.json, rally_analysis.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
