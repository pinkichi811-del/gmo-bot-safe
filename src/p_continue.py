"""p_continue モデル。

「保有を続けたら +X*ATR に先に触るか」の確率を返す。
Phase 1: 状態バケット統計
Phase 2: ロジスティック回帰（stdlib のみ、バッチ勾配降下）

両方同じインターフェースを持ち、backtest 側で差し替え可能。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ----------------------------------------------------------------------
# 特徴量
# ----------------------------------------------------------------------
FEATURE_NAMES = (
    "bars_held",
    "unrealized_atr",       # (price - entry) / atr_at_entry
    "peak_atr",             # (peak - entry) / atr_at_entry
    "drawdown_from_peak",   # (price - peak) / peak (<=0)
    "ret_3bar",
    "ret_5bar",
    "ma_slope_norm",        # (MA20 - MA20_5bars_ago) / price
    "rsi",
    "volume_ratio",
)


@dataclass
class Sample:
    features: dict[str, float]
    label: int  # 1 if +X*ATR hit first within N bars, else 0


# ----------------------------------------------------------------------
# Phase 1: バケット統計
# ----------------------------------------------------------------------
class BucketPContinue:
    """離散化した特徴量バケットでの P(label=1) を直接計算する。

    過学習回避のためバケット数を抑える。スパースなバケットは親バケット→全体平均に後退。
    """

    def __init__(self) -> None:
        self._prob: dict[tuple, float] = {}
        self._count: dict[tuple, int] = {}
        self._global_mean: float = 0.5
        self._n_total: int = 0

    @staticmethod
    def _bucket(feat: dict[str, float]) -> tuple:
        # 3 次元 × 各 3 段階 = 27 バケット
        bh = feat["bars_held"]
        held_b = 0 if bh < 5 else (1 if bh < 15 else 2)
        ur = feat["unrealized_atr"]
        ur_b = 0 if ur < 0 else (1 if ur < 1.0 else 2)
        rs = feat["rsi"]
        rsi_b = 0 if rs < 50 else (1 if rs < 70 else 2)
        return (held_b, ur_b, rsi_b)

    def fit(self, samples: list[Sample]) -> None:
        if not samples:
            self._global_mean = 0.5
            return
        totals: dict[tuple, int] = defaultdict(int)
        pos: dict[tuple, int] = defaultdict(int)
        for s in samples:
            b = self._bucket(s.features)
            totals[b] += 1
            if s.label == 1:
                pos[b] += 1
        self._count = dict(totals)
        # Laplace スムージング（1 勝 1 敗プラス）でスパース吸収
        self._prob = {
            b: (pos[b] + 1) / (totals[b] + 2) for b in totals
        }
        self._n_total = sum(totals.values())
        self._global_mean = sum(pos.values()) / self._n_total

    def predict(self, features: dict[str, float]) -> float:
        b = self._bucket(features)
        n = self._count.get(b, 0)
        if n < 20:
            # サンプル不足 → 全体平均へ後退
            return self._global_mean
        return self._prob.get(b, self._global_mean)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "bucket",
            "global_mean": self._global_mean,
            "buckets": [
                {"key": list(k), "count": self._count[k], "prob": self._prob[k]}
                for k in self._count
            ],
        }


# ----------------------------------------------------------------------
# Phase 2: ロジスティック回帰（stdlib のみ）
# ----------------------------------------------------------------------
def _sigmoid(z: float) -> float:
    if z < -500:
        return 0.0
    if z > 500:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


class LogisticPContinue:
    """バッチ勾配降下による簡易 LR。numpy 不要、stdlib のみ。"""

    def __init__(
        self,
        features: tuple[str, ...] = FEATURE_NAMES,
        iters: int = 300,
        lr: float = 0.3,
        l2: float = 0.01,
    ) -> None:
        self.feature_names = features
        self.iters = iters
        self.lr = lr
        self.l2 = l2
        self.w: list[float] = [0.0] * len(features)
        self.b: float = 0.0
        self.mean: list[float] = [0.0] * len(features)
        self.std: list[float] = [1.0] * len(features)
        self.trained = False

    def _vec(self, feat: dict[str, float]) -> list[float]:
        return [float(feat.get(name, 0.0)) for name in self.feature_names]

    def _normalize(self, X: list[list[float]]) -> list[list[float]]:
        nrows, ncols = len(X), len(X[0])
        means = [0.0] * ncols
        for row in X:
            for j in range(ncols):
                means[j] += row[j]
        means = [m / nrows for m in means]
        stds = [0.0] * ncols
        for row in X:
            for j in range(ncols):
                stds[j] += (row[j] - means[j]) ** 2
        stds = [max(math.sqrt(s / nrows), 1e-6) for s in stds]
        self.mean = means
        self.std = stds
        return [[(row[j] - means[j]) / stds[j] for j in range(ncols)] for row in X]

    def fit(self, samples: list[Sample]) -> None:
        if not samples:
            self.trained = False
            return
        X = [self._vec(s.features) for s in samples]
        y = [s.label for s in samples]
        Xn = self._normalize(X)
        n, d = len(Xn), len(Xn[0])
        self.w = [0.0] * d
        self.b = 0.0
        for _ in range(self.iters):
            gw = [0.0] * d
            gb = 0.0
            for i in range(n):
                z = self.b + sum(self.w[j] * Xn[i][j] for j in range(d))
                p = _sigmoid(z)
                err = p - y[i]
                for j in range(d):
                    gw[j] += err * Xn[i][j]
                gb += err
            for j in range(d):
                self.w[j] -= self.lr * (gw[j] / n + self.l2 * self.w[j])
            self.b -= self.lr * gb / n
        self.trained = True

    def predict(self, features: dict[str, float]) -> float:
        if not self.trained:
            return 0.5
        x = self._vec(features)
        xn = [(x[j] - self.mean[j]) / self.std[j] for j in range(len(x))]
        z = self.b + sum(self.w[j] * xn[j] for j in range(len(xn)))
        return _sigmoid(z)

    def to_json(self) -> dict[str, Any]:
        return {
            "type": "logistic",
            "features": list(self.feature_names),
            "w": self.w, "b": self.b,
            "mean": self.mean, "std": self.std,
            "trained": self.trained,
        }


# ----------------------------------------------------------------------
# サンプル生成: バックテスト内で使う
# ----------------------------------------------------------------------
def build_features(
    entry_price: float,
    entry_atr: float,
    bars_held: int,
    current_candles: list,  # 現在 bar までの candle 系列（entry 前の履歴含む）
    peak_price: float,
) -> dict[str, float]:
    """現在 bar の時点での特徴量辞書を生成する。"""
    cur = current_candles[-1]
    closes = [c.close for c in current_candles]

    unrealized = (cur.close - entry_price)
    unrealized_atr = unrealized / entry_atr if entry_atr > 0 else 0.0
    peak_atr = (peak_price - entry_price) / entry_atr if entry_atr > 0 else 0.0
    drawdown_from_peak = (cur.close - peak_price) / peak_price if peak_price > 0 else 0.0

    if len(closes) >= 4:
        ret_3 = (closes[-1] - closes[-4]) / closes[-4]
    else:
        ret_3 = 0.0
    if len(closes) >= 6:
        ret_5 = (closes[-1] - closes[-6]) / closes[-6]
    else:
        ret_5 = 0.0

    # MA(20) の 5 bar slope
    if len(closes) >= 25:
        ma_now = sum(closes[-20:]) / 20
        ma_5ago = sum(closes[-25:-5]) / 20
        ma_slope_norm = (ma_now - ma_5ago) / cur.close if cur.close > 0 else 0.0
    else:
        ma_slope_norm = 0.0

    # RSI
    from entry_v2 import rsi as _rsi
    rsi_val = _rsi(closes, 14)

    # volume ratio: 直近 20 本平均との比
    if len(current_candles) >= 20:
        avg_v = sum(c.volume for c in current_candles[-20:]) / 20
        vol_ratio = cur.volume / avg_v if avg_v > 0 else 0.0
    else:
        vol_ratio = 1.0

    return {
        "bars_held": float(bars_held),
        "unrealized_atr": unrealized_atr,
        "peak_atr": peak_atr,
        "drawdown_from_peak": drawdown_from_peak,
        "ret_3bar": ret_3,
        "ret_5bar": ret_5,
        "ma_slope_norm": ma_slope_norm,
        "rsi": rsi_val,
        "volume_ratio": vol_ratio,
    }


def label_hit_upside_first(
    future_candles: list,  # 評価対象の bar より未来
    current_price: float,
    atr_value: float,
    up_atr: float = 1.0,
    down_atr: float = 0.8,
    horizon_n: int = 8,
) -> int | None:
    """次の horizon_n 本以内に +up_atr*ATR 到達が先なら 1、逆なら 0。
    データ不足で未到達なら None（サンプル除外）。"""
    if atr_value <= 0 or len(future_candles) < horizon_n:
        return None
    up_thr = current_price + up_atr * atr_value
    dn_thr = current_price - down_atr * atr_value
    for i in range(min(horizon_n, len(future_candles))):
        c = future_candles[i]
        hit_up = c.high >= up_thr
        hit_dn = c.low <= dn_thr
        if hit_up and hit_dn:
            # 同一 bar で両方: 保守的に下値到達を優先
            return 0
        if hit_up:
            return 1
        if hit_dn:
            return 0
    return None  # どちらも未到達はサンプルに含めない
