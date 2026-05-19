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

    def test_on_order_reject_increments_and_halts(self) -> None:
        """3 回連発で HALT、それ未満では HALT しない。"""
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            g = RiskGuard(BASE_CFG, state)
            self.assertEqual(state.order_reject_count(), 0)
            g.on_order_reject("a")
            self.assertEqual(state.order_reject_count(), 1)
            self.assertFalse(g.is_halted())
            g.on_order_reject("b")
            self.assertEqual(state.order_reject_count(), 2)
            self.assertFalse(g.is_halted())
            g.on_order_reject("c")
            self.assertEqual(state.order_reject_count(), 3)
            self.assertTrue(g.is_halted())
            self.assertIn("order_rejects", state.halt_reason())

    def test_on_success_resets_reject_count(self) -> None:
        """サイクル成功でカウンタがゼロに戻る (連続性が切れる)。"""
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            g = RiskGuard(BASE_CFG, state)
            g.on_order_reject("a")
            g.on_order_reject("b")
            self.assertEqual(state.order_reject_count(), 2)
            g.on_success()
            self.assertEqual(state.order_reject_count(), 0)
            # リセット後の reject は 1 から再カウント
            g.on_order_reject("c")
            self.assertEqual(state.order_reject_count(), 1)
            self.assertFalse(g.is_halted())

    def test_reject_and_error_counters_are_independent(self) -> None:
        """on_order_reject と on_error のカウンタが独立。

        一方の閾値到達でもう一方が HALT を引き起こさないこと、また両方の
        閾値を別々に持つこと。
        """
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            g = RiskGuard(BASE_CFG, state)
            # error を 2 回 (閾値 5 に未到達)、reject を 2 回 (閾値 3 に未到達)
            g.on_error(RuntimeError("e1"))
            g.on_error(RuntimeError("e2"))
            g.on_order_reject("r1")
            g.on_order_reject("r2")
            self.assertEqual(state.error_count(), 2)
            self.assertEqual(state.order_reject_count(), 2)
            self.assertFalse(g.is_halted())
            # reject が 3 回目で HALT (error は閾値未到達のまま)
            g.on_order_reject("r3")
            self.assertTrue(g.is_halted())
            self.assertIn("order_rejects", state.halt_reason())
            self.assertEqual(state.error_count(), 2)  # error カウンタは独立

    def test_custom_max_order_rejects(self) -> None:
        """config の `max_order_rejects_consecutive` で閾値が変わる。"""
        with tempfile.TemporaryDirectory() as td:
            state = self._state(td)
            cfg = {
                **BASE_CFG,
                "risk": {**BASE_CFG["risk"], "max_order_rejects_consecutive": 1},
            }
            g = RiskGuard(cfg, state)
            g.on_order_reject("only_one")
            self.assertTrue(g.is_halted())

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

    def test_code_gate_is_open_after_phase5(self) -> None:
        """Phase 5 で gate1 (ENABLE_LIVE_ORDER) は **意図的に True**。

        旧名: test_code_gate_is_closed_by_default。Phase 4 までは「False の
        ままであること」を assert していたが、Phase 5 の単独 PR で True に
        切り替わった。本テストは「Phase 5 以降は意図的に True」を確認する
        反転 assert として残す (履歴 trace)。
        """
        import order_executor as oe
        self.assertTrue(oe.ENABLE_LIVE_ORDER,
                        "Phase 5 で ENABLE_LIVE_ORDER は True に変更済み")

    def test_live_blocked_by_code_gate(self) -> None:
        """Phase 5 で gate1 は常時 True だが、テスト内で一時的に False に
        戻すと code_gate でブロックされることを確認する (回帰防止)。

        運用上 ENABLE_LIVE_ORDER を False に戻すのは緊急停止の唯一の手段。
        その経路が壊れていないことを保証する意味で残す。
        """
        import order_executor as oe
        old_code = oe.ENABLE_LIVE_ORDER
        oe.ENABLE_LIVE_ORDER = False
        try:
            with tempfile.TemporaryDirectory() as td:
                os.environ["STATE_DIR"] = td
                ex = OrderExecutor(BASE_CFG, mode="live")
                result = ex.execute(self._decision())
                self.assertEqual(result["status"], "blocked_by_code_gate")
        finally:
            oe.ENABLE_LIVE_ORDER = old_code
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
                self.assertEqual(result["status"], "no_order_client")
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


class TestExitLogic(unittest.TestCase):
    """trail_pct / max_hold_bars と exit 優先順の確認。"""

    EXITS_CFG: dict = {
        **BASE_CFG,
        "exits": {
            "stop_loss_pct": -4.0, "take_profit_pct": 6.0, "cooldown_min": 180,
            "trail_pct": 3.0, "max_hold_bars": 288,
        },
        "loop": {"max_orders_per_cycle": 2, "watch_interval_sec": 300},
    }

    def _state_with_pos(
        self, td: str, entry: float = 10_000_000.0,
        highest: float | None = None, entry_ts: float | None = None,
    ) -> StateStore:
        import time
        s = StateStore(path=str(Path(td) / "state.json"))
        s.set_position(Position(
            "BTC_JPY", 10000.0, entry,
            entry_ts if entry_ts is not None else time.time(),
            highest if highest is not None else entry,
        ))
        return s

    def _snap(self, last: float, ts: float | None = None) -> MarketSnapshot:
        import time
        from market_watcher import Ticker
        t = ts if ts is not None else time.time()
        return MarketSnapshot(
            ts=t,
            tickers={"BTC_JPY": Ticker("BTC_JPY", last, last - 1, last + 1, 1.0, t)},
        )

    def test_trail_fires_from_peak(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = self._state_with_pos(td, highest=10_500_000)
            g = RiskGuard(self.EXITS_CFG, state)
            # 現在値 10.15M → peak 10.5M から -3.33% 下落、entry 10M から +1.5%
            decisions = g.evaluate_sells(self._snap(10_150_000))
            self.assertEqual(len(decisions), 1)
            self.assertIn("trail", decisions[0].reason)

    def test_trail_skipped_when_peak_at_entry(self) -> None:
        """peak が entry と同じ（未上昇）なら trail は発火しない。"""
        with tempfile.TemporaryDirectory() as td:
            state = self._state_with_pos(td, highest=10_000_000)
            g = RiskGuard(self.EXITS_CFG, state)
            decisions = g.evaluate_sells(self._snap(9_800_000))  # -2%
            self.assertEqual(len(decisions), 0)

    def test_max_hold_fires_after_limit(self) -> None:
        import time
        with tempfile.TemporaryDirectory() as td:
            # 288 bars × 300s = 86400s = 24h. 25h 保有で発火。
            state = self._state_with_pos(
                td, entry_ts=time.time() - 86400 - 3600,
            )
            g = RiskGuard(self.EXITS_CFG, state)
            decisions = g.evaluate_sells(self._snap(10_100_000))
            self.assertEqual(len(decisions), 1)
            self.assertIn("max_hold", decisions[0].reason)

    def test_priority_sl_over_trail(self) -> None:
        """SL 条件が勝つ。trail も同時に満たしていても SL が先。"""
        with tempfile.TemporaryDirectory() as td:
            state = self._state_with_pos(td, highest=10_500_000)
            g = RiskGuard(self.EXITS_CFG, state)
            decisions = g.evaluate_sells(self._snap(9_500_000))  # SL -5%
            self.assertEqual(len(decisions), 1)
            self.assertIn("stop_loss", decisions[0].reason)

    def test_priority_trail_over_tp(self) -> None:
        """TP と trail 両方満たすとき trail が勝つ（backtest と同じ）。"""
        with tempfile.TemporaryDirectory() as td:
            state = self._state_with_pos(td, highest=11_000_000)
            g = RiskGuard(self.EXITS_CFG, state)
            # 10.65M: entry +6.5% (TP 条件満たす), peak 11M から -3.18% (trail 条件満たす)
            decisions = g.evaluate_sells(self._snap(10_650_000))
            self.assertEqual(len(decisions), 1)
            self.assertIn("trail", decisions[0].reason)

    def test_trail_pct_zero_disables(self) -> None:
        cfg = {**self.EXITS_CFG,
               "exits": {**self.EXITS_CFG["exits"], "trail_pct": 0.0}}
        with tempfile.TemporaryDirectory() as td:
            state = self._state_with_pos(td, highest=10_500_000)
            g = RiskGuard(cfg, state)
            decisions = g.evaluate_sells(self._snap(10_150_000))
            self.assertEqual(len(decisions), 0)

    def test_max_hold_zero_disables(self) -> None:
        import time
        cfg = {**self.EXITS_CFG,
               "exits": {**self.EXITS_CFG["exits"], "max_hold_bars": 0,
                         "trail_pct": 0.0}}
        with tempfile.TemporaryDirectory() as td:
            state = self._state_with_pos(
                td, entry_ts=time.time() - 86400 - 3600,
            )
            g = RiskGuard(cfg, state)
            decisions = g.evaluate_sells(self._snap(10_100_000))
            self.assertEqual(len(decisions), 0)

    def test_highest_px_updates_during_sell_eval(self) -> None:
        """新高値が来れば pos.highest_px も更新される。"""
        with tempfile.TemporaryDirectory() as td:
            state = self._state_with_pos(td, highest=10_100_000)
            g = RiskGuard(self.EXITS_CFG, state)
            g.evaluate_sells(self._snap(10_500_000))
            pos = state.positions()["BTC_JPY"]
            self.assertEqual(pos.highest_px, 10_500_000)

    def test_position_from_dict_legacy_has_no_highest_px(self) -> None:
        """既存 state.json (highest_px なし) でも読めて entry_price にフォールバック。"""
        d = {"symbol": "BTC_JPY", "size_jpy": 10000.0,
             "entry_price": 5_000_000.0, "entry_ts": 100.0}
        p = Position.from_dict(d)
        self.assertEqual(p.highest_px, 5_000_000.0)


class TestRegimeGate(unittest.TestCase):
    """main.build_regime_gate の構築と挙動。"""

    @staticmethod
    def _write_ndx_csv(path: Path, n: int = 50, rising: bool = True) -> None:
        """n 日分の擬似 NDX daily CSV を書き出す。"""
        lines = ["ts,date,open,high,low,close"]
        for i in range(n):
            ts = 1_000_000 + i * 86400
            close = (100 + i) if rising else (100 + (n - i))  # 上昇 or 下降
            lines.append(
                f"{ts},2026-01-{(i % 28) + 1:02d},"
                f"{close},{close + 1},{close - 1},{close}"
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_returns_none_when_not_configured(self) -> None:
        from main import build_regime_gate
        self.assertIsNone(build_regime_gate({}))

    def test_returns_none_when_disabled(self) -> None:
        from main import build_regime_gate
        self.assertIsNone(
            build_regime_gate({"regime_filter": {"enabled": False}})
        )

    def test_returns_none_when_csv_missing(self) -> None:
        from main import build_regime_gate
        gate = build_regime_gate({
            "regime_filter": {
                "enabled": True,
                "ndx_trend": {
                    "enabled": True,
                    "csv_path": "/nonexistent/path/NDX.csv",
                    "ma_short": 5, "ma_long": 10,
                },
            },
        })
        self.assertIsNone(gate)

    def test_uptrend_allows_buy(self) -> None:
        from main import build_regime_gate
        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "NDX.csv"
            self._write_ndx_csv(csv, n=50, rising=True)
            gate = build_regime_gate({
                "regime_filter": {
                    "enabled": True,
                    "ndx_trend": {
                        "enabled": True, "csv_path": str(csv),
                        "ma_short": 5, "ma_long": 10,
                    },
                },
            })
            self.assertIsNotNone(gate)
            # 30 日目相当: 単調増加なので ma_short > ma_long
            allow, reason = gate(1_000_000 + 30 * 86400)
            self.assertTrue(allow)
            self.assertEqual(reason, "")

    def test_downtrend_blocks_buy(self) -> None:
        from main import build_regime_gate
        with tempfile.TemporaryDirectory() as td:
            csv = Path(td) / "NDX.csv"
            self._write_ndx_csv(csv, n=50, rising=False)
            gate = build_regime_gate({
                "regime_filter": {
                    "enabled": True,
                    "ndx_trend": {
                        "enabled": True, "csv_path": str(csv),
                        "ma_short": 5, "ma_long": 10,
                    },
                },
            })
            self.assertIsNotNone(gate)
            allow, reason = gate(1_000_000 + 30 * 86400)
            self.assertFalse(allow)
            self.assertIn("ndx_trend", reason)


if __name__ == "__main__":
    unittest.main()
