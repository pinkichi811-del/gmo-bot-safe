"""dry-run 実行に必要な壊れやすい部分の最低限チェック。

stdlib の unittest のみ使用（追加依存なし）。実行:
    python -m unittest discover tests
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_watcher import (  # noqa: E402
    MarketSnapshot,
    MarketWatcher,
    StubMarketDataSource,
)
from order_executor import OrderExecutor  # noqa: E402
from risk_guard import BuyVerdict, Decision, RiskGuard  # noqa: E402
from scorer import Score, Scorer, apply_cash_bonus  # noqa: E402
from state_store import Position, StateStore  # noqa: E402


BASE_CFG: dict = {
    "symbols": {"core": ["BTC_JPY", "ETH_JPY"], "satellite": ["SOL_JPY"]},
    "limits": {
        "max_positions": 3, "min_cash_ratio": 0.20,
        "max_core_ratio": 0.35, "max_sat_ratio": 0.25,
    },
    "risk": {
        "halt_on_error": True, "max_consecutive_errors": 5,
        "halt_on_price_gap_pct": 10.0, "per_trade_jpy_max": 10000,
        "block_buy_below_cash_ratio": 0.20, "stop_file": "./STOP_TEST",
    },
    "scorer": {
        "ai_enabled": True, "ai_weight": 0.3, "rule_weight": 0.7,
        "thresholds": {
            "buy_candidate": {"total": 70, "trend": 18, "liquidity": 10, "heat": -8},
            "strong_buy": {"total": 78, "trend": 22, "liquidity": 12, "heat": -5},
            "dup_penalty_block": -8,
        },
        "volatility": {"low_threshold_pct": 1.0, "high_threshold_pct": 5.0,
                       "max_penalty": 10.0},
        "spread": {"tight_threshold_pct": 0.05, "wide_threshold_pct": 0.5,
                   "max_penalty": 10.0},
        "rsi": {"period": 14, "overbought": 75, "oversold": 25,
                "max_overbought_penalty": 5.0, "max_oversold_bonus": 3.0},
        "cash_bonus": {"high_threshold": 0.5, "mid_threshold": 0.3,
                       "high_bonus": 5.0, "mid_bonus": 2.0},
    },
    "exits": {"stop_loss_pct": -4.0, "take_profit_pct": 6.0, "cooldown_min": 180},
    "loop": {"max_orders_per_cycle": 2},
    "portfolio": {"initial_cash_jpy": 1_000_000},
}


class TestConfigShape(unittest.TestCase):
    """本物の config/app.yaml が dry-run に必要なキーを持つこと。"""

    def test_real_config_has_required_keys(self) -> None:
        try:
            import yaml  # type: ignore
        except ImportError:
            self.skipTest("PyYAML not installed")
        cfg_path = ROOT / "config" / "app.yaml"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        for key in ("symbols", "limits", "risk", "scorer", "exits", "loop"):
            self.assertIn(key, cfg)
        self.assertIn("buy_candidate", cfg["scorer"]["thresholds"])
        self.assertIn("strong_buy", cfg["scorer"]["thresholds"])


class TestStubWatcher(unittest.TestCase):
    def test_returns_all_symbols_with_enough_candles(self) -> None:
        w = MarketWatcher(BASE_CFG, source=StubMarketDataSource(seed=42))
        snap = w.fetch()
        self.assertEqual(set(snap.symbols()), {"BTC_JPY", "ETH_JPY", "SOL_JPY"})
        for sym in snap.symbols():
            # scorer は 20 本以上必要
            self.assertGreaterEqual(len(snap.ohlcv[sym]), 20)
            tk = snap.tickers[sym]
            self.assertGreater(tk.last, 0)
            self.assertLess(tk.bid, tk.ask)


class TestScorer(unittest.TestCase):
    def test_produces_required_fields(self) -> None:
        w = MarketWatcher(BASE_CFG, source=StubMarketDataSource(seed=42))
        snap = w.fetch()
        sc = Scorer(BASE_CFG)
        scores = sc.score(snap)
        self.assertGreater(len(scores), 0)
        for s in scores:
            for field in ("total", "trend", "liquidity", "heat",
                          "volatility", "dup_penalty", "cash_bonus",
                          "rule_score", "ai_score"):
                self.assertTrue(hasattr(s, field))

    def test_volatility_is_non_positive(self) -> None:
        """安全寄り: volatility は減点のみ（+にならない）。"""
        w = MarketWatcher(BASE_CFG, source=StubMarketDataSource(seed=42))
        snap = w.fetch()
        sc = Scorer(BASE_CFG)
        for s in sc.score(snap):
            self.assertLessEqual(s.volatility, 0.0)

    def test_cash_bonus_applies(self) -> None:
        sc = Scorer(BASE_CFG)
        w = MarketWatcher(BASE_CFG, source=StubMarketDataSource(seed=42))
        scores = sc.score(w.fetch())
        base_totals = [s.total for s in scores]
        apply_cash_bonus(scores, cash_ratio=0.8, cfg=BASE_CFG)  # high_bonus
        for s, base in zip(scores, base_totals):
            self.assertGreater(s.cash_bonus, 0.0)
            self.assertAlmostEqual(s.total, base + s.cash_bonus, places=5)

    def test_cash_bonus_zero_when_low(self) -> None:
        sc = Scorer(BASE_CFG)
        w = MarketWatcher(BASE_CFG, source=StubMarketDataSource(seed=42))
        scores = sc.score(w.fetch())
        apply_cash_bonus(scores, cash_ratio=0.1, cfg=BASE_CFG)
        for s in scores:
            self.assertEqual(s.cash_bonus, 0.0)


class TestRiskGuard(unittest.TestCase):
    def _state(self, td: str) -> StateStore:
        return StateStore(path=str(Path(td) / "state.json"))

    def test_health_check_halts_on_empty_tickers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            g = RiskGuard(BASE_CFG, state)
            self.assertFalse(g.health_check(MarketSnapshot()))
            self.assertTrue(g.is_halted())

    def test_buy_verdict_reports_threshold_miss(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            g = RiskGuard(BASE_CFG, state)
            bad = Score("BTC_JPY", total=50, trend=5, liquidity=5, heat=5,
                        dup_penalty=0, rule_score=50, ai_score=0)
            passed, verdicts = g.evaluate_buy_candidates([bad])
            self.assertEqual(len(passed), 0)
            self.assertEqual(len(verdicts), 1)
            self.assertFalse(verdicts[0].passes)
            self.assertIn("below_threshold", verdicts[0].reason)

    def test_buy_verdict_passes_strong(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            g = RiskGuard(BASE_CFG, state)
            good = Score("BTC_JPY", total=100, trend=25, liquidity=15, heat=0,
                         dup_penalty=0, rule_score=100, ai_score=0)
            passed, verdicts = g.evaluate_buy_candidates([good])
            self.assertEqual(len(passed), 1)
            self.assertTrue(verdicts[0].passes)
            self.assertTrue(verdicts[0].strong)

    def test_sell_stop_loss_fires(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            state.set_position(Position("BTC_JPY", 10000.0, 100.0, 0.0))
            g = RiskGuard(BASE_CFG, state)
            from market_watcher import Ticker
            snap = MarketSnapshot(
                ts=0.0,
                tickers={"BTC_JPY": Ticker("BTC_JPY", 90.0, 89.9, 90.1, 10.0, 0.0)},
            )
            decisions = g.evaluate_sells(snap)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].side, "sell")
            self.assertIn("stop_loss", decisions[0].reason)


class TestOrderExecutor(unittest.TestCase):
    def test_dry_run_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            os.environ["STATE_DIR"] = td
            try:
                ex = OrderExecutor(BASE_CFG, mode="dry_run")
                d = Decision(
                    symbol="BTC_JPY", side="buy", size_jpy=10000,
                    price_ref=100.0, reason="test", strong=False,
                )
                ex.execute(d)
                jsonl = Path(td) / "dry_run_orders.jsonl"
                self.assertTrue(jsonl.exists())
                lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 1)
                rec = json.loads(lines[0])
                self.assertEqual(rec["symbol"], "BTC_JPY")
                self.assertEqual(rec["mode"], "dry_run")
            finally:
                os.environ.pop("STATE_DIR", None)


class TestLiveGates(unittest.TestCase):
    """live の三段ゲートを確認する。どの段階でも確実に注文が送信されないこと。"""

    def _decision(self) -> Decision:
        return Decision(
            symbol="BTC_JPY", side="buy", size_jpy=10000,
            price_ref=100.0, reason="test", strong=False,
        )

    def test_code_gate_is_closed_by_default(self) -> None:
        import order_executor as oe
        self.assertFalse(oe.ENABLE_LIVE_ORDER,
                         "ENABLE_LIVE_ORDER は False のままであること")

    def test_live_blocked_by_code_gate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            os.environ["STATE_DIR"] = td
            try:
                ex = OrderExecutor(BASE_CFG, mode="live")
                result = ex.execute(self._decision())
                self.assertEqual(result["status"], "blocked_by_code_gate")
            finally:
                os.environ.pop("STATE_DIR", None)

    def test_live_blocked_by_env_gate(self) -> None:
        import order_executor as oe
        old_code = oe.ENABLE_LIVE_ORDER
        old_env = os.environ.pop("LIVE_OK", None)
        oe.ENABLE_LIVE_ORDER = True
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["STATE_DIR"] = td
                ex = OrderExecutor(BASE_CFG, mode="live")
                result = ex.execute(self._decision())
                self.assertEqual(result["status"], "blocked_by_env_gate")
        finally:
            oe.ENABLE_LIVE_ORDER = old_code
            if old_env is not None:
                os.environ["LIVE_OK"] = old_env
            os.environ.pop("STATE_DIR", None)

    def test_live_blocked_by_not_implemented(self) -> None:
        """両ゲート通過でも _send_live_order 未実装で止まること。"""
        import order_executor as oe
        old_code = oe.ENABLE_LIVE_ORDER
        old_env = os.environ.get("LIVE_OK")
        oe.ENABLE_LIVE_ORDER = True
        os.environ["LIVE_OK"] = "yes"
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["STATE_DIR"] = td
                ex = OrderExecutor(BASE_CFG, mode="live")
                result = ex.execute(self._decision())
                self.assertEqual(result["status"], "not_implemented")
        finally:
            oe.ENABLE_LIVE_ORDER = old_code
            if old_env is None:
                os.environ.pop("LIVE_OK", None)
            else:
                os.environ["LIVE_OK"] = old_env
            os.environ.pop("STATE_DIR", None)


class TestStateStore(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            s1 = StateStore(path=str(p))
            s1.set_position(Position("BTC_JPY", 10000.0, 5_000_000.0, 0.0))
            s1.save()
            s2 = StateStore(path=str(p))
            self.assertTrue(s2.has_position("BTC_JPY"))

    def test_cooldown_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            s = StateStore(path=str(Path(td) / "state.json"))
            s.set_cooldown("BTC_JPY", minutes=10)
            self.assertTrue(s.in_cooldown("BTC_JPY"))
            remaining = s.cooldown_remaining_sec("BTC_JPY")
            self.assertGreater(remaining, 0)
            self.assertLessEqual(remaining, 10 * 60 + 1)

    def test_cooldown_verdict_includes_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = StateStore(path=str(Path(td) / "state.json"))
            state.set_cooldown("BTC_JPY", minutes=30)
            g = RiskGuard(BASE_CFG, state)
            good = Score("BTC_JPY", total=100, trend=25, liquidity=15, heat=0,
                         dup_penalty=0, rule_score=100, ai_score=0)
            _, verdicts = g.evaluate_buy_candidates([good])
            self.assertIn("cooldown", verdicts[0].reason)
            self.assertIn("min_left", verdicts[0].reason)

    def test_halt_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "state.json"
            s1 = StateStore(path=str(p))
            s1.set_halt("test_reason")
            s2 = StateStore(path=str(p))
            self.assertTrue(s2.is_halted())
            self.assertEqual(s2.halt_reason(), "test_reason")


if __name__ == "__main__":
    unittest.main()
