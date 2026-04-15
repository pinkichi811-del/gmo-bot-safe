"""リスクガード。

- HALT / STOP 管理
- データ健全性チェック
- 売り候補判定（損切り・利確）
- 買い候補フィルタ（閾値・重複ペナルティ・クールダウン）
- ポートフォリオ制約適用（同時保有数・現金比率・core/sat 比率・1サイクル上限）

すべての発注はこのモジュールを通すこと。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_watcher import MarketSnapshot
from scorer import Score
from state_store import StateStore

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    symbol: str
    side: str           # "buy" | "sell"
    size_jpy: float
    price_ref: float
    reason: str
    strong: bool = False


@dataclass
class BuyVerdict:
    """買い候補としての判定結果。観察ログ用に見送り理由も保持する。"""
    symbol: str
    passes: bool
    strong: bool
    reason: str         # "buy" / "strong_buy" / "already_held" / "cooldown" /
                        # "dup_penalty(...)" / "below_threshold:..."


class RiskGuard:
    def __init__(self, cfg: dict[str, Any], state: StateStore) -> None:
        self.cfg = cfg
        self.state = state

        limits = cfg.get("limits", {}) or {}
        self.max_positions: int = int(limits.get("max_positions", 3))
        self.min_cash_ratio: float = float(limits.get("min_cash_ratio", 0.20))
        self.max_core_ratio: float = float(limits.get("max_core_ratio", 0.35))
        self.max_sat_ratio: float = float(limits.get("max_sat_ratio", 0.25))

        risk = cfg.get("risk", {}) or {}
        self.halt_on_error: bool = bool(risk.get("halt_on_error", True))
        self.max_consec_errors: int = int(risk.get("max_consecutive_errors", 5))
        self.halt_on_price_gap_pct: float = float(risk.get("halt_on_price_gap_pct", 10.0))
        self.per_trade_jpy_max: float = float(risk.get("per_trade_jpy_max", 10000))
        self.block_buy_below_cash_ratio: float = float(
            risk.get("block_buy_below_cash_ratio", 0.20)
        )
        self.stop_file: Path = Path(risk.get("stop_file", "./STOP"))

        thr = (cfg.get("scorer", {}) or {}).get("thresholds", {}) or {}
        buy = thr.get("buy_candidate", {}) or {}
        strong = thr.get("strong_buy", {}) or {}
        self.buy_total = float(buy.get("total", 70))
        self.buy_trend = float(buy.get("trend", 18))
        self.buy_liq = float(buy.get("liquidity", 10))
        self.buy_heat = float(buy.get("heat", -8))
        self.strong_total = float(strong.get("total", 78))
        self.strong_trend = float(strong.get("trend", 22))
        self.strong_liq = float(strong.get("liquidity", 12))
        self.strong_heat = float(strong.get("heat", -5))
        self.dup_block = float(thr.get("dup_penalty_block", -8))

        exits = cfg.get("exits", {}) or {}
        self.stop_loss_pct: float = float(exits.get("stop_loss_pct", -4.0))
        self.take_profit_pct: float = float(exits.get("take_profit_pct", 6.0))
        self.cooldown_min: float = float(exits.get("cooldown_min", 180))
        self.trail_pct: float = float(exits.get("trail_pct", 0.0) or 0.0)
        self.max_hold_bars: int = int(exits.get("max_hold_bars", 0) or 0)

        loop_cfg = cfg.get("loop", {}) or {}
        self.bar_sec: float = float(loop_cfg.get("watch_interval_sec", 300))

        symbols = cfg.get("symbols", {}) or {}
        self.core_symbols: set[str] = set(symbols.get("core", []))
        self.sat_symbols: set[str] = set(symbols.get("satellite", []))

        loop = cfg.get("loop", {}) or {}
        self.max_orders_per_cycle: int = int(loop.get("max_orders_per_cycle", 2))

    # ------------------------------------------------------------------
    # HALT / STOP
    # ------------------------------------------------------------------
    def is_halted(self) -> bool:
        return self.state.is_halted()

    def is_stop_file_active(self) -> bool:
        exists = self.stop_file.exists()
        if exists:
            logger.info("STOP file detected at %s", self.stop_file)
        return exists

    def halt(self, reason: str) -> None:
        logger.error("HALT triggered: %s", reason)
        self.state.set_halt(reason)

    def on_error(self, err: Exception) -> None:
        count = self.state.increment_error()
        logger.warning("risk_guard error_count=%d err=%r", count, err)
        if self.halt_on_error and count >= self.max_consec_errors:
            self.halt(f"consecutive_errors>={self.max_consec_errors}")

    def on_success(self) -> None:
        if self.state.error_count() > 0:
            logger.info("resetting error_count (was %d)", self.state.error_count())
        self.state.reset_errors()

    # ------------------------------------------------------------------
    # 健全性チェック
    # ------------------------------------------------------------------
    def health_check(self, snapshot: MarketSnapshot) -> bool:
        if not snapshot.tickers:
            self.halt("empty_tickers")
            return False
        for sym, tk in snapshot.tickers.items():
            if tk.last <= 0 or tk.bid <= 0 or tk.ask <= 0:
                self.halt(f"invalid_price:{sym}")
                return False
            if tk.ask < tk.bid:
                self.halt(f"crossed_book:{sym}")
                return False
            candles = snapshot.ohlcv.get(sym, [])
            if candles:
                prev = candles[-1].close
                if prev <= 0:
                    self.halt(f"invalid_candle:{sym}")
                    return False
                gap_pct = abs(tk.last - prev) / prev * 100.0
                if gap_pct > self.halt_on_price_gap_pct:
                    self.halt(f"price_gap:{sym}:{gap_pct:.2f}%")
                    return False
        return True

    # ------------------------------------------------------------------
    # 売り判定（損切り・利確）
    # ------------------------------------------------------------------
    def evaluate_sells(self, snapshot: MarketSnapshot) -> list[Decision]:
        decisions: list[Decision] = []
        now = snapshot.ts or 0.0
        for sym, pos in self.state.positions().items():
            tk = snapshot.tickers.get(sym)
            if tk is None or pos.entry_price <= 0:
                continue
            # 最高値 track（trail 用）: 先に更新してから判定する
            self.state.update_highest_px(sym, tk.last)
            highest = max(pos.highest_px or pos.entry_price, tk.last)

            pct = (tk.last - pos.entry_price) / pos.entry_price * 100.0

            # 優先順: SL → trail → TP → max_hold (backtest と揃える)
            if pct <= self.stop_loss_pct:
                decisions.append(Decision(
                    symbol=sym, side="sell", size_jpy=pos.size_jpy,
                    price_ref=tk.last, reason=f"stop_loss {pct:+.2f}%",
                ))
                continue

            if self.trail_pct > 0 and highest > pos.entry_price:
                trail_drop = (tk.last - highest) / highest * 100.0
                if trail_drop <= -self.trail_pct:
                    decisions.append(Decision(
                        symbol=sym, side="sell", size_jpy=pos.size_jpy,
                        price_ref=tk.last,
                        reason=f"trail {trail_drop:+.2f}% from peak",
                    ))
                    continue

            if pct >= self.take_profit_pct:
                decisions.append(Decision(
                    symbol=sym, side="sell", size_jpy=pos.size_jpy,
                    price_ref=tk.last, reason=f"take_profit {pct:+.2f}%",
                ))
                continue

            if self.max_hold_bars > 0 and pos.entry_ts > 0 and self.bar_sec > 0:
                held_bars = (now - pos.entry_ts) / self.bar_sec
                if held_bars >= self.max_hold_bars:
                    decisions.append(Decision(
                        symbol=sym, side="sell", size_jpy=pos.size_jpy,
                        price_ref=tk.last,
                        reason=f"max_hold {held_bars:.0f}bars pnl={pct:+.2f}%",
                    ))
                    continue
        return decisions

    # ------------------------------------------------------------------
    # 買い候補フィルタ
    # ------------------------------------------------------------------
    def _meets_buy(self, s: Score) -> bool:
        return (
            s.total >= self.buy_total
            and s.trend >= self.buy_trend
            and s.liquidity >= self.buy_liq
            and s.heat >= self.buy_heat
        )

    def _meets_strong(self, s: Score) -> bool:
        return (
            s.total >= self.strong_total
            and s.trend >= self.strong_trend
            and s.liquidity >= self.strong_liq
            and s.heat >= self.strong_heat
        )

    def _threshold_miss_reason(self, s: Score) -> str:
        misses: list[str] = []
        if s.total < self.buy_total:
            misses.append(f"total({s.total:.1f}<{self.buy_total:.0f})")
        if s.trend < self.buy_trend:
            misses.append(f"trend({s.trend:.1f}<{self.buy_trend:.0f})")
        if s.liquidity < self.buy_liq:
            misses.append(f"liquidity({s.liquidity:.1f}<{self.buy_liq:.0f})")
        if s.heat < self.buy_heat:
            misses.append(f"heat({s.heat:.1f}<{self.buy_heat:.0f})")
        return "below_threshold:" + ",".join(misses) if misses else "below_threshold"

    def _evaluate_buy_one(self, s: Score) -> BuyVerdict:
        if self.state.has_position(s.symbol):
            return BuyVerdict(s.symbol, False, False, "already_held")
        if self.state.in_cooldown(s.symbol):
            remaining_min = self.state.cooldown_remaining_sec(s.symbol) / 60.0
            return BuyVerdict(
                s.symbol, False, False,
                f"cooldown({remaining_min:.0f}min_left)",
            )
        if s.dup_penalty <= self.dup_block:
            return BuyVerdict(
                s.symbol, False, False,
                f"dup_penalty({s.dup_penalty:.1f}<={self.dup_block:.1f})",
            )
        if not self._meets_buy(s):
            return BuyVerdict(s.symbol, False, False, self._threshold_miss_reason(s))
        strong = self._meets_strong(s)
        return BuyVerdict(s.symbol, True, strong, "strong_buy" if strong else "buy")

    def evaluate_buy_candidates(
        self, scores: list[Score],
    ) -> tuple[list[Score], list[BuyVerdict]]:
        """各銘柄を評価し、(通過した Score, 全件の判定) を返す。

        観察ログ用に、見送られた銘柄の理由もすべて返す。
        """
        passed: list[Score] = []
        verdicts: list[BuyVerdict] = []
        for s in scores:
            v = self._evaluate_buy_one(s)
            verdicts.append(v)
            if v.passes:
                passed.append(s)
            else:
                logger.debug("skip %s: %s", s.symbol, v.reason)
        return passed, verdicts

    # ------------------------------------------------------------------
    # ポートフォリオ制約
    # ------------------------------------------------------------------
    def apply_portfolio_constraints(
        self,
        candidates: list[Score],
        snapshot: MarketSnapshot,
        cash_jpy: float,
        total_equity_jpy: float,
    ) -> tuple[list[Decision], dict[str, str]]:
        """通過済み候補にポートフォリオ制約を適用し、(決定, 見送り理由dict) を返す。"""
        decisions: list[Decision] = []
        rejections: dict[str, str] = {}
        held_count = len(self.state.positions())
        remaining_cash = cash_jpy

        # 強い買い候補を優先
        ordered = sorted(
            candidates,
            key=lambda s: (self._meets_strong(s), s.total),
            reverse=True,
        )

        for s in ordered:
            if len(decisions) >= self.max_orders_per_cycle:
                rejections[s.symbol] = (
                    f"max_orders_per_cycle({self.max_orders_per_cycle})"
                )
                continue
            if held_count + len(decisions) >= self.max_positions:
                rejections[s.symbol] = f"max_positions({self.max_positions})"
                continue

            tk = snapshot.tickers.get(s.symbol)
            if tk is None:
                rejections[s.symbol] = "no_ticker"
                continue

            size_jpy = min(self.per_trade_jpy_max, remaining_cash)
            if size_jpy <= 0:
                rejections[s.symbol] = "no_cash"
                continue

            # core / satellite 比率キャップ
            if s.symbol in self.core_symbols:
                cap_ratio = self.max_core_ratio
            elif s.symbol in self.sat_symbols:
                cap_ratio = self.max_sat_ratio
            else:
                rejections[s.symbol] = "unknown_group"
                continue

            if total_equity_jpy > 0:
                max_by_ratio = total_equity_jpy * cap_ratio
                if size_jpy > max_by_ratio:
                    logger.info("clip %s to cap_ratio=%.2f (%.0f→%.0f)",
                                s.symbol, cap_ratio, size_jpy, max_by_ratio)
                    size_jpy = max_by_ratio

            if size_jpy <= 0:
                rejections[s.symbol] = "size_zero_after_cap"
                continue

            # 約定後の現金比率チェック
            if total_equity_jpy > 0:
                post_cash = remaining_cash - size_jpy
                post_ratio = post_cash / total_equity_jpy
                if post_ratio < self.block_buy_below_cash_ratio:
                    rejections[s.symbol] = (
                        f"cash_ratio({post_ratio:.2f}<"
                        f"{self.block_buy_below_cash_ratio:.2f})"
                    )
                    continue

            strong = self._meets_strong(s)
            decisions.append(Decision(
                symbol=s.symbol,
                side="buy",
                size_jpy=size_jpy,
                price_ref=tk.last,
                reason=(
                    ("strong_buy" if strong else "buy")
                    + f" total={s.total:.1f} trend={s.trend:.1f} liq={s.liquidity:.1f}"
                ),
                strong=strong,
            ))
            remaining_cash -= size_jpy

        return decisions, rejections
