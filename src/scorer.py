"""スコアリング。

ルールベースのスコア（trend / liquidity / heat / volatility / dup_penalty）を合成し、
AI 補助スコア（現状スタブ）を加える。最終的な発注可否は risk_guard に委ねる。

設計上の定数はすべて `config/app.yaml` の `scorer:` 以下から読む。
ハードコードしないこと（観察フェーズの調整を config 編集だけで回すため）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from market_watcher import Candle, MarketSnapshot, Ticker

logger = logging.getLogger(__name__)


@dataclass
class Score:
    symbol: str
    total: float = 0.0
    trend: float = 0.0
    liquidity: float = 0.0
    heat: float = 0.0
    volatility: float = 0.0
    dup_penalty: float = 0.0
    cash_bonus: float = 0.0
    rule_score: float = 0.0
    ai_score: float = 0.0

    def as_log(self) -> str:
        return (
            f"{self.symbol} total={self.total:.1f} trend={self.trend:.1f} "
            f"liq={self.liquidity:.1f} heat={self.heat:.1f} vol={self.volatility:.1f} "
            f"dup={self.dup_penalty:.1f} cash={self.cash_bonus:.1f} "
            f"rule={self.rule_score:.1f} ai={self.ai_score:.1f}"
        )


class Scorer:
    def __init__(self, cfg: dict[str, Any]) -> None:
        sc = cfg.get("scorer", {}) or {}

        self.base_score: float = float(sc.get("base_score", 60.0))
        self.ai_enabled: bool = bool(sc.get("ai_enabled", False))
        self.ai_weight: float = float(sc.get("ai_weight", 0.3))
        self.rule_weight: float = float(sc.get("rule_weight", 0.7))

        symbols = cfg.get("symbols", {}) or {}
        self.core_symbols: set[str] = set(symbols.get("core", []))
        self.satellite_symbols: set[str] = set(symbols.get("satellite", []))

        # trend
        t = sc.get("trend") or {}
        self.trend_short_ma: int = int(t.get("short_ma", 5))
        self.trend_long_ma: int = int(t.get("long_ma", 20))
        self.trend_ratio_clamp: float = float(t.get("ratio_clamp", 0.05))
        self.trend_max_magnitude: float = float(t.get("max_magnitude", 30.0))

        # liquidity
        lq = sc.get("liquidity") or {}
        self.liq_window: int = int(lq.get("window", 10))
        self.liq_volume_divisor: float = float(lq.get("volume_divisor", 5.0))
        self.liq_max_score: float = float(lq.get("max_score", 20.0))

        # heat
        h = sc.get("heat") or {}
        self.heat_window: int = int(h.get("window", 5))
        self.heat_up_threshold: float = float(h.get("up_threshold_pct", 5.0)) / 100.0
        self.heat_down_threshold: float = float(h.get("down_threshold_pct", 5.0)) / 100.0
        self.heat_up_scale: float = float(h.get("up_scale_pct", 10.0)) / 100.0
        self.heat_down_scale: float = float(h.get("down_scale_pct", 10.0)) / 100.0
        self.heat_up_max_penalty: float = float(h.get("up_max_penalty", 20.0))
        self.heat_down_max_penalty: float = float(h.get("down_max_penalty", 10.0))
        self.heat_neutral_bonus: float = float(h.get("neutral_bonus", 5.0))

        # volatility
        vol_cfg = sc.get("volatility", {}) or {}
        self.vol_window: int = int(vol_cfg.get("window", 20))
        self.vol_low_pct: float = float(vol_cfg.get("low_threshold_pct", 1.0))
        self.vol_high_pct: float = float(vol_cfg.get("high_threshold_pct", 5.0))
        self.vol_max_penalty: float = float(vol_cfg.get("max_penalty", 10.0))

        # spread (liquidity に後付け)
        spread_cfg = sc.get("spread", {}) or {}
        self.spread_tight_pct: float = float(spread_cfg.get("tight_threshold_pct", 0.05))
        self.spread_wide_pct: float = float(spread_cfg.get("wide_threshold_pct", 0.5))
        self.spread_max_penalty: float = float(spread_cfg.get("max_penalty", 10.0))

        # rsi (heat に後付け)
        rsi_cfg = sc.get("rsi", {}) or {}
        self.rsi_period: int = int(rsi_cfg.get("period", 14))
        self.rsi_overbought: float = float(rsi_cfg.get("overbought", 75))
        self.rsi_oversold: float = float(rsi_cfg.get("oversold", 25))
        self.rsi_max_overbought_pen: float = float(
            rsi_cfg.get("max_overbought_penalty", 5.0)
        )
        self.rsi_max_oversold_bonus: float = float(
            rsi_cfg.get("max_oversold_bonus", 3.0)
        )

        # dup_penalty
        dp = sc.get("dup_penalty") or {}
        self.dup_same_symbol: float = float(dp.get("same_symbol", -15.0))
        self.dup_same_group: float = float(dp.get("same_group", -5.0))

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def score(
        self,
        snapshot: MarketSnapshot,
        held_symbols: Iterable[str] = (),
    ) -> list[Score]:
        held = set(held_symbols)
        results: list[Score] = []
        min_candles = max(self.trend_long_ma, self.heat_window, self.liq_window, 10)
        for sym in snapshot.symbols():
            candles = snapshot.ohlcv.get(sym, [])
            if len(candles) < min_candles:
                logger.debug("scorer: not enough candles for %s (%d<%d)",
                             sym, len(candles), min_candles)
                continue
            ticker = snapshot.tickers.get(sym)
            trend = self._trend_score(candles)
            liquidity = self._liquidity_score(candles, ticker)
            heat = self._heat_score(candles)
            volatility = self._volatility_score(candles)
            dup = self._dup_penalty(sym, held)
            rule = self._combine_rule(trend, liquidity, heat, volatility, dup)
            ai = self._ai_score(sym, snapshot) if self.ai_enabled else 0.0
            if self.ai_enabled and ai != 0.0:
                total = rule * self.rule_weight + ai * self.ai_weight
            else:
                total = rule
            results.append(Score(
                symbol=sym,
                total=total,
                trend=trend,
                liquidity=liquidity,
                heat=heat,
                volatility=volatility,
                dup_penalty=dup,
                rule_score=rule,
                ai_score=ai,
            ))
        results.sort(key=lambda s: s.total, reverse=True)
        return results

    # ------------------------------------------------------------------
    # サブスコア
    # ------------------------------------------------------------------
    def _trend_score(self, candles: list[Candle]) -> float:
        closes = [c.close for c in candles]
        if len(closes) < self.trend_long_ma:
            return 0.0
        short_ma = sum(closes[-self.trend_short_ma:]) / self.trend_short_ma
        long_ma = sum(closes[-self.trend_long_ma:]) / self.trend_long_ma
        if long_ma <= 0:
            return 0.0
        ratio = (short_ma - long_ma) / long_ma
        clamp = max(self.trend_ratio_clamp, 1e-9)
        return (
            max(min(ratio / clamp, 1.0), -1.0) * self.trend_max_magnitude
        )

    def _liquidity_score(
        self, candles: list[Candle], ticker: Ticker | None,
    ) -> float:
        if not candles:
            return 0.0
        window = self.liq_window
        vols = [c.volume for c in candles[-window:]]
        avg = sum(vols) / len(vols) if vols else 0.0
        divisor = max(self.liq_volume_divisor, 1e-9)
        vol_score = min(avg / divisor, self.liq_max_score)

        if ticker is None or ticker.last <= 0:
            return vol_score
        spread_abs = ticker.ask - ticker.bid
        if spread_abs <= 0:
            return vol_score
        spread_pct = spread_abs / ticker.last * 100.0
        if spread_pct <= self.spread_tight_pct:
            return vol_score
        rng = max(self.spread_wide_pct - self.spread_tight_pct, 1e-9)
        ratio = min((spread_pct - self.spread_tight_pct) / rng, 1.0)
        return vol_score - ratio * self.spread_max_penalty

    def _heat_score(self, candles: list[Candle]) -> float:
        if len(candles) < self.heat_window:
            return 0.0
        recent = candles[-self.heat_window:]
        open_px = recent[0].open
        if open_px <= 0:
            return 0.0
        chg = (recent[-1].close - open_px) / open_px
        if chg > self.heat_up_threshold:
            up_scale = max(self.heat_up_scale, 1e-9)
            base = -self.heat_up_max_penalty * min(chg / up_scale, 1.0)
        elif chg < -self.heat_down_threshold:
            down_scale = max(self.heat_down_scale, 1e-9)
            base = -self.heat_down_max_penalty * min(abs(chg) / down_scale, 1.0)
        else:
            base = self.heat_neutral_bonus

        rsi = self._rsi([c.close for c in candles], self.rsi_period)
        if rsi > self.rsi_overbought:
            span = max(100.0 - self.rsi_overbought, 1e-9)
            adj = -min((rsi - self.rsi_overbought) / span, 1.0) * self.rsi_max_overbought_pen
        elif rsi < self.rsi_oversold:
            span = max(self.rsi_oversold, 1e-9)
            adj = min((self.rsi_oversold - rsi) / span, 1.0) * self.rsi_max_oversold_bonus
        else:
            adj = 0.0
        return base + adj

    def _volatility_score(self, candles: list[Candle]) -> float:
        closes = [c.close for c in candles[-self.vol_window:]]
        if len(closes) < 2:
            return 0.0
        returns: list[float] = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                returns.append((closes[i] - closes[i - 1]) / closes[i - 1])
        if len(returns) < 2:
            return 0.0
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / len(returns)
        std_pct = (var ** 0.5) * 100.0
        if std_pct <= self.vol_low_pct:
            return 0.0
        rng = max(self.vol_high_pct - self.vol_low_pct, 1e-9)
        ratio = min((std_pct - self.vol_low_pct) / rng, 1.0)
        return -ratio * self.vol_max_penalty

    @staticmethod
    def _rsi(closes: list[float], period: int) -> float:
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

    def _dup_penalty(self, symbol: str, held: set[str]) -> float:
        if symbol in held:
            return self.dup_same_symbol
        if symbol in self.core_symbols and (held & self.core_symbols):
            return self.dup_same_group
        if symbol in self.satellite_symbols and (held & self.satellite_symbols):
            return self.dup_same_group
        return 0.0

    def _combine_rule(
        self, trend: float, liquidity: float, heat: float,
        volatility: float, dup: float,
    ) -> float:
        return self.base_score + trend + liquidity + heat + volatility + dup

    def _ai_score(self, symbol: str, snapshot: MarketSnapshot) -> float:
        """AI 補助スコア。

        TODO(live): LLM / ML モデルからのシグナルを反映する。
                    AI 単体で発注を決めさせない設計は変えない。
        """
        return 0.0


# ----------------------------------------------------------------------
# ポートフォリオ側から後付けする bonus / penalty
# ----------------------------------------------------------------------
def apply_cash_bonus(
    scores: list[Score],
    cash_ratio: float,
    cfg: dict[str, Any],
) -> list[Score]:
    """現金余力が十分なら小さなボーナスを全銘柄に加える（観察用）。"""
    cb = ((cfg.get("scorer") or {}).get("cash_bonus") or {})
    high_thr = float(cb.get("high_threshold", 0.5))
    mid_thr = float(cb.get("mid_threshold", 0.3))
    high_bonus = float(cb.get("high_bonus", 5.0))
    mid_bonus = float(cb.get("mid_bonus", 2.0))

    if cash_ratio >= high_thr:
        bonus = high_bonus
    elif cash_ratio >= mid_thr:
        bonus = mid_bonus
    else:
        bonus = 0.0

    for s in scores:
        s.cash_bonus = bonus
        s.rule_score += bonus
        s.total += bonus
    return scores
