"""Entry v3 — BTC 専用の 2 戦略。

rally 分析（data/analysis/rally_analysis.md）で判明した事実をベースに設計:
  - 5min 単独では rally / drop を識別できない
  - 唯一弱く効く軸は「ボラ圧縮」(atr_percentile 低)
  - 方向性 edge は上位足から持ってくる必要がある

EntryV3A: Volatility-Compression-Only
  5min のみ。圧縮環境に入った時だけ entry。direction を当てる気は無く
  symmetric な risk 管理で対処する想定。

EntryV3B: Multi-Timeframe Trend + 5min Compression (本命)
  1H で bullish を確認し、5min で圧縮＋MA20 上を合わせた時のみ entry。

どちらも PRECOMPUTE された indicators dict を受け取って O(1) で判定する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EntryV3Signal:
    triggered: bool
    reason: str = ""
    atr_at_entry: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


class EntryV3A:
    """Volatility-compression-only. 5min only.

    パラメータ（config.entry_v3a）:
      atr_pct_max:            atr_percentile がこれ未満で entry 許可
      range_width_atr_max:    直近 20 本の high-low / ATR がこれ以下
      upper_wick_ratio_max:   直近 bar の上ヒゲ比がこれ未満
      rsi_max:                RSI がこれ未満
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        ev = (cfg.get("entry_v3a") or {})
        self.atr_pct_max = float(ev.get("atr_pct_max", 30.0))
        self.range_width_atr_max = float(ev.get("range_width_atr_max", 4.0))
        self.upper_wick_max = float(ev.get("upper_wick_ratio_max", 0.2))
        self.rsi_max = float(ev.get("rsi_max", 70.0))

    def evaluate(self, i: int, ind_5m: dict[str, list]) -> EntryV3Signal:
        atr = ind_5m["atr"][i]
        if atr <= 0:
            return EntryV3Signal(False, "no_atr")

        atr_pct = ind_5m["atr_pct"][i]
        if atr_pct >= self.atr_pct_max:
            return EntryV3Signal(
                False, f"atr_pct_high {atr_pct:.0f}>={self.atr_pct_max:.0f}", atr,
                details={"atr_pct": atr_pct},
            )

        rw = ind_5m["range_width"][i]
        if rw > self.range_width_atr_max:
            return EntryV3Signal(
                False, f"range_wide {rw:.1f}>{self.range_width_atr_max:.1f}", atr,
                details={"range_width_atr": rw},
            )

        uw = ind_5m["upper_wick"][i]
        if uw >= self.upper_wick_max:
            return EntryV3Signal(
                False, f"upper_wick {uw:.2f}", atr,
                details={"upper_wick_ratio": uw},
            )

        r = ind_5m["rsi"][i]
        if r >= self.rsi_max:
            return EntryV3Signal(
                False, f"rsi_high {r:.0f}", atr,
                details={"rsi": r},
            )

        return EntryV3Signal(
            True, "compression_entry", atr,
            details={"atr_pct": atr_pct, "range_width_atr": rw,
                     "upper_wick": uw, "rsi": r},
        )


class EntryV3B:
    """Multi-timeframe: 1H で bullish + 5min 圧縮 + 5min close>MA20。

    パラメータ（config.entry_v3b）:
      htf_rsi_min:            1H RSI がこれ以上
      atr_pct_max:            5min atr_percentile がこれ未満
      require_1h_above_sma50: 1H close > 1H SMA(50) を要求する
      require_5m_above_sma20: 5min close > 5min SMA(20) を要求する
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        ev = (cfg.get("entry_v3b") or {})
        self.htf_rsi_min = float(ev.get("htf_rsi_min", 50.0))
        self.atr_pct_max = float(ev.get("atr_pct_max", 40.0))
        self.require_1h_above_sma50 = bool(ev.get("require_1h_above_sma50", True))
        self.require_5m_above_sma20 = bool(ev.get("require_5m_above_sma20", True))

    def evaluate(
        self, i: int, ind_5m: dict[str, list],
        ind_1h: dict[str, list], map_5m_to_1h: list[int],
    ) -> EntryV3Signal:
        atr = ind_5m["atr"][i]
        if atr <= 0:
            return EntryV3Signal(False, "no_atr")

        # 5min close > MA20
        close_5m = ind_5m["closes"][i]
        sma20 = ind_5m["sma20"][i]
        if self.require_5m_above_sma20:
            if sma20 <= 0 or close_5m <= sma20:
                return EntryV3Signal(
                    False, "below_sma20_5m", atr,
                    details={"close_5m": close_5m, "sma20": sma20},
                )

        # 5min 圧縮
        atr_pct = ind_5m["atr_pct"][i]
        if atr_pct >= self.atr_pct_max:
            return EntryV3Signal(
                False, f"atr_pct_high {atr_pct:.0f}", atr,
                details={"atr_pct": atr_pct},
            )

        # 1H context
        j = map_5m_to_1h[i]
        if j < 0:
            return EntryV3Signal(False, "no_1h_yet", atr)

        close_1h = ind_1h["closes"][j]
        sma50_1h = ind_1h["sma50"][j]
        rsi_1h = ind_1h["rsi"][j]

        if self.require_1h_above_sma50:
            if sma50_1h <= 0 or close_1h <= sma50_1h:
                return EntryV3Signal(
                    False, "below_sma50_1h", atr,
                    details={"close_1h": close_1h, "sma50_1h": sma50_1h},
                )

        if rsi_1h < self.htf_rsi_min:
            return EntryV3Signal(
                False, f"rsi_1h {rsi_1h:.0f}<{self.htf_rsi_min:.0f}", atr,
                details={"rsi_1h": rsi_1h},
            )

        return EntryV3Signal(
            True, "mtf_bullish_compression", atr,
            details={
                "atr_pct": atr_pct, "rsi_1h": rsi_1h,
                "close_5m": close_5m, "sma20_5m": sma20,
                "close_1h": close_1h, "sma50_1h": sma50_1h,
            },
        )


# ----------------------------------------------------------------------
# 共通 exit
# ----------------------------------------------------------------------
class SharedExit:
    """v2 系の safety rails を p_continue 無しで切り出したもの。

    パラメータ（config.shared_exit）:
      hard_stop_pct:          建値からこの % 割れで即 exit
      trailing_stop_pct:      ピークからこの % 押しで exit
      trailing_activate_atr:  +XX × ATR の含み益を出してから trailing 有効化
      max_hold_bars:          この本数経過で exit
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        ev = (cfg.get("shared_exit") or {})
        self.hard_stop_pct = float(ev.get("hard_stop_pct", 2.5))
        self.trailing_stop_pct = float(ev.get("trailing_stop_pct", 1.5))
        self.trailing_activate_atr = float(ev.get("trailing_activate_atr", 0.5))
        self.max_hold_bars = int(ev.get("max_hold_bars", 48))

    def evaluate(
        self, entry_price: float, entry_atr: float,
        current_price: float, peak_price: float, bars_held: int,
    ) -> tuple[str | None, float | None]:
        # hard stop
        if entry_price > 0:
            drop_pct = (current_price - entry_price) / entry_price * 100.0
            if drop_pct <= -self.hard_stop_pct:
                return ("hard_stop", drop_pct)
        # trailing
        if peak_price > entry_price and entry_atr > 0:
            peak_atr = (peak_price - entry_price) / entry_atr
            if peak_atr >= self.trailing_activate_atr:
                retrace_pct = (current_price - peak_price) / peak_price * 100.0
                if retrace_pct <= -self.trailing_stop_pct:
                    return ("trailing_stop", retrace_pct)
        # max hold
        if bars_held >= self.max_hold_bars:
            return ("max_hold", float(bars_held))
        return (None, None)
