"""Entry signal v2 — BTC 専用。

設計:
  Regime filter (遅い・ゲート) → Trigger (速い・発火) → Quality filter (ノイズ除去)
  の 3 段フィルタ。全段通過で初めて entry 候補。

最終判断権は必ず risk_guard（ルールエンジン）が握る。この層は候補生成のみ。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from market_watcher import Candle


# ----------------------------------------------------------------------
# 指標計算（外部依存なし）
# ----------------------------------------------------------------------
def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def atr(candles: list[Candle], n: int = 14) -> float | None:
    if len(candles) < n + 1:
        return None
    trs: list[float] = []
    for i in range(-n, 0):
        c = candles[i]
        pc = candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - pc), abs(c.low - pc))
        trs.append(tr)
    return sum(trs) / n


def rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gains += diff
        else:
            losses += -diff
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    rs = (gains / period) / (losses / period)
    return 100.0 - (100.0 / (1.0 + rs))


# ----------------------------------------------------------------------
# Entry シグナル
# ----------------------------------------------------------------------
@dataclass
class EntrySignal:
    triggered: bool
    reason: str = ""
    atr_at_entry: float = 0.0
    regime_ok: bool = False
    trigger_fired: bool = False
    quality_ok: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class EntryV2:
    """BTC 向け 3 段フィルタ entry。

    候補パラメータ一覧（config.entry_v2 配下）:
      regime_ma_fast:          上昇 regime の速い MA（例 50）
      regime_ma_slow:          上昇 regime の遅い MA（例 200）
      regime_atr_max_pct:      ATR/価格 がこの % を超えると regime NG（過熱判定）
      regime_rsi_max:          RSI がこれを超えると regime NG
      breakout_window_bars:    過去 N 本の高値更新で breakout 発火
      momentum_min_atr_frac:   直近 bar の (close-open)/ATR がこの値以上で momentum 補助 OK
      quality_rsi_max:         entry 時点の RSI 上限
      quality_volume_lookback: 出来高比較のルックバック本数
      quality_volume_min_ratio: 直近出来高 / 平均出来高 の最低倍率
      quality_peak_distance_pct: 直近 peak からこの % 以内にいると天井追い扱いで除外
      atr_period:              ATR の期間
      rsi_period:              RSI の期間
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        ev = (cfg.get("entry_v2") or {})
        # regime
        self.regime_ma_fast = int(ev.get("regime_ma_fast", 50))
        self.regime_ma_slow = int(ev.get("regime_ma_slow", 200))
        self.regime_atr_max_pct = float(ev.get("regime_atr_max_pct", 2.0))
        self.regime_rsi_max = float(ev.get("regime_rsi_max", 78.0))
        # trigger
        self.breakout_window_bars = int(ev.get("breakout_window_bars", 20))
        self.momentum_min_atr_frac = float(ev.get("momentum_min_atr_frac", 0.3))
        # quality
        self.quality_rsi_max = float(ev.get("quality_rsi_max", 72.0))
        self.quality_volume_lookback = int(ev.get("quality_volume_lookback", 20))
        self.quality_volume_min_ratio = float(ev.get("quality_volume_min_ratio", 1.1))
        self.quality_peak_distance_pct = float(ev.get("quality_peak_distance_pct", 0.3))
        # indicator params
        self.atr_period = int(ev.get("atr_period", 14))
        self.rsi_period = int(ev.get("rsi_period", 14))

    def evaluate(self, candles: list[Candle]) -> EntrySignal:
        need = max(self.regime_ma_slow, self.breakout_window_bars,
                   self.quality_volume_lookback, self.atr_period + 1,
                   self.rsi_period + 1)
        if len(candles) < need:
            return EntrySignal(False, reason="insufficient_data")

        closes = [c.close for c in candles]
        last = candles[-1]

        # --- Regime filter ---
        ma_f = sma(closes, self.regime_ma_fast) or 0.0
        ma_s = sma(closes, self.regime_ma_slow) or 0.0
        a = atr(candles, self.atr_period) or 0.0
        r = rsi(closes, self.rsi_period)
        atr_pct = (a / last.close * 100.0) if last.close > 0 else 0.0
        regime_up = ma_f > ma_s and last.close > ma_s
        regime_vol_ok = atr_pct <= self.regime_atr_max_pct
        regime_rsi_ok = r <= self.regime_rsi_max
        regime_ok = regime_up and regime_vol_ok and regime_rsi_ok

        details: dict[str, Any] = {
            "ma_fast": ma_f, "ma_slow": ma_s, "atr": a,
            "atr_pct": atr_pct, "rsi": r,
            "regime_up": regime_up, "regime_vol_ok": regime_vol_ok,
            "regime_rsi_ok": regime_rsi_ok,
        }

        if not regime_ok:
            return EntrySignal(
                False, reason="regime_ng", atr_at_entry=a,
                regime_ok=False, details=details,
            )

        # --- Trigger: breakout or strong momentum bar ---
        window = candles[-(self.breakout_window_bars + 1):-1]
        prior_high = max(c.high for c in window) if window else last.high
        breakout = last.close > prior_high
        bar_range = max(a, 1e-9)
        bar_move = (last.close - last.open) / bar_range
        strong_bar = bar_move >= self.momentum_min_atr_frac
        trigger_fired = breakout or strong_bar

        details.update({
            "prior_high": prior_high, "breakout": breakout,
            "bar_move_atr": bar_move, "strong_bar": strong_bar,
        })

        if not trigger_fired:
            return EntrySignal(
                False, reason="trigger_none", atr_at_entry=a,
                regime_ok=True, trigger_fired=False, details=details,
            )

        # --- Quality filter ---
        quality_rsi_ok = r <= self.quality_rsi_max
        vol_window = candles[-self.quality_volume_lookback:]
        avg_vol = sum(c.volume for c in vol_window) / len(vol_window) if vol_window else 0.0
        vol_ratio = (last.volume / avg_vol) if avg_vol > 0 else 0.0
        quality_vol_ok = vol_ratio >= self.quality_volume_min_ratio
        # 天井追い抑制: 直近 N 本の peak から何 % 離れているか
        peak = max(c.high for c in window) if window else last.high
        distance_pct = (peak - last.close) / last.close * 100.0 if last.close > 0 else 0.0
        quality_not_top = distance_pct >= -self.quality_peak_distance_pct
        # breakout 時は peak 超えて当然なのでこの条件は無効化
        if breakout:
            quality_not_top = True

        quality_ok = quality_rsi_ok and quality_vol_ok and quality_not_top

        details.update({
            "quality_rsi_ok": quality_rsi_ok,
            "volume_ratio": vol_ratio,
            "quality_vol_ok": quality_vol_ok,
            "peak_distance_pct": distance_pct,
            "quality_not_top": quality_not_top,
        })

        if not quality_ok:
            return EntrySignal(
                False, reason="quality_ng", atr_at_entry=a,
                regime_ok=True, trigger_fired=True, quality_ok=False,
                details=details,
            )

        reason = "breakout" if breakout else "momentum_bar"
        return EntrySignal(
            True, reason=reason, atr_at_entry=a,
            regime_ok=True, trigger_fired=True, quality_ok=True,
            details=details,
        )
