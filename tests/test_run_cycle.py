"""run_cycle 全体の結合テスト。

multi-symbol / regime gate / STOP file / HALT スキップ / 売り発生など、
sub-component を組み合わせたときの挙動を確認する。
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

from main import run_cycle  # noqa: E402
from market_watcher import MarketWatcher, StubMarketDataSource  # noqa: E402
from notifier import Notifier  # noqa: E402
from order_executor import OrderExecutor  # noqa: E402
from risk_guard import RiskGuard  # noqa: E402
from scorer import Scorer  # noqa: E402
from state_store import Position, StateStore  # noqa: E402


CFG_BTC_ETH: dict = {
    "symbols": {"core": ["BTC_JPY", "ETH_JPY"], "satellite": []},
    "limits": {
        "max_watch": 5, "max_positions": 1, "min_cash_ratio": 0.20,
        "max_core_ratio": 0.50, "max_sat_ratio": 0.25,
    },
    "loop": {
        "watch_interval_sec": 300, "score_interval_sec": 300,
        "max_orders_per_cycle": 1,
    },
    "scorer": {
        "ai_enabled": False, "ai_weight": 0.3, "rule_weight": 0.7,
        "base_score": 60.0,
        "trend": {"short_ma": 5, "long_ma": 20, "ratio_clamp": 0.05,
                  "max_magnitude": 30.0},
        "liquidity": {"window": 10, "volume_divisor": 5.0, "max_score": 20.0},
        "heat": {"window": 5, "up_threshold_pct": 5.0, "down_threshold_pct": 5.0,
                 "up_scale_pct": 10.0, "down_scale_pct": 10.0,
                 "up_max_penalty": 20.0, "down_max_penalty": 10.0,
                 "neutral_bonus": 5.0},
        "dup_penalty": {"same_symbol": -15.0, "same_group": -5.0},
        "thresholds": {
            "buy_candidate": {"total": 70, "trend": 5, "liquidity": 0, "heat": -8},
            "strong_buy": {"total": 78, "trend": 10, "liquidity": 5, "heat": -5},
            "dup_penalty_block": -8,
        },
        "volatility": {"window": 20, "low_threshold_pct": 1.0,
                       "high_threshold_pct": 5.0, "max_penalty": 10.0},
        "spread": {"tight_threshold_pct": 0.05, "wide_threshold_pct": 0.5,
                   "max_penalty": 10.0},
        "rsi": {"period": 14, "overbought": 75, "oversold": 25,
                "max_overbought_penalty": 5.0, "max_oversold_bonus": 3.0},
        "cash_bonus": {"high_threshold": 0.5, "mid_threshold": 0.3,
                       "high_bonus": 5.0, "mid_bonus": 2.0},
    },
    "exits": {"stop_loss_pct": -4.0, "take_profit_pct": 6.0, "cooldown_min": 0},
    "risk": {
        "halt_on_error": True, "max_consecutive_errors": 5,
        "halt_on_price_gap_pct": 50.0, "per_trade_jpy_max": 10_000,
        "block_buy_below_cash_ratio": 0.20, "stop_file": "./STOP_TEST",
    },
    "portfolio": {"initial_cash_jpy": 1_000_000},
    "notifier": {"enabled": False, "on_halt": True, "on_order": True, "on_error": True},
}


class _StaticPriceSource(StubMarketDataSource):
    """Stub の上に「価格を上書きする」薄いラッパ。

    sell 判定（SL/TP）を確実に発火させたいときに使う。
    """

    def __init__(self, overrides: dict[str, float]) -> None:
        super().__init__(seed=42)
        self._overrides = overrides

    def fetch_tickers(self, symbols):  # type: ignore[override]
        out = super().fetch_tickers(symbols)
        for sym, px in self._overrides.items():
            if sym in out:
                tk = out[sym]
                spread = px * 0.0005
                out[sym].last = px
                out[sym].bid = px - spread
                out[sym].ask = px + spread
        return out


def _read_cycle_log(state_dir: Path) -> list[dict]:
    log_dir = state_dir / "score_log"
    if not log_dir.exists():
        return []
    records: list[dict] = []
    for f in sorted(log_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


class TestRunCycleIntegration(unittest.TestCase):
    def _components(
        self,
        state_dir: Path,
        cfg: dict | None = None,
        source=None,
    ) -> tuple[StateStore, MarketWatcher, Scorer, RiskGuard, OrderExecutor, Notifier]:
        os.environ["STATE_DIR"] = str(state_dir)
        c = cfg or CFG_BTC_ETH
        state = StateStore(path=str(state_dir / "state.json"))
        watcher = MarketWatcher(c, source=source or StubMarketDataSource(seed=42))
        scorer = Scorer(c)
        guard = RiskGuard(c, state)
        executor = OrderExecutor(c, mode="dry_run")
        notifier = Notifier(c)
        return state, watcher, scorer, guard, executor, notifier

    def setUp(self) -> None:
        self._old_state_dir = os.environ.get("STATE_DIR")

    def tearDown(self) -> None:
        if self._old_state_dir is None:
            os.environ.pop("STATE_DIR", None)
        else:
            os.environ["STATE_DIR"] = self._old_state_dir

    # ------------------------------------------------------------------
    # 通常: 多銘柄 stub で 1 サイクル回ると評価が記録される
    # ------------------------------------------------------------------
    def test_multi_symbol_cycle_records_evaluations(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            state, w, sc, g, ex, n = self._components(sd)
            run_cycle(CFG_BTC_ETH, state, w, sc, g, ex, n, regime_gate=None)
            recs = _read_cycle_log(sd)
            self.assertEqual(len(recs), 1)
            evals = recs[0]["evaluations"]
            symbols = {e["symbol"] for e in evals}
            self.assertEqual(symbols, {"BTC_JPY", "ETH_JPY"})

    # ------------------------------------------------------------------
    # regime gate がブロックすると buys=0、sells は通常評価
    # ------------------------------------------------------------------
    def test_regime_block_suppresses_buys_only(self) -> None:
        # 既保有 BTC_JPY、価格を SL ライン以下に → sell 発火するはず
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            state, w, sc, g, ex, n = self._components(
                sd, source=_StaticPriceSource({"BTC_JPY": 9_000_000.0}),
            )
            state.set_position(Position("BTC_JPY", 10_000.0, 10_000_000.0, 0.0))

            def gate(_ts: float) -> tuple[bool, str]:
                return False, "regime_blocked:test"

            run_cycle(CFG_BTC_ETH, state, w, sc, g, ex, n, regime_gate=gate)
            recs = _read_cycle_log(sd)
            self.assertEqual(len(recs), 1)
            sides = [d["side"] for d in recs[0]["decisions"]]
            self.assertNotIn("buy", sides)
            # 価格 -10% で SL (-4%) を割っているので sell が出る
            self.assertIn("sell", sides)
            verdicts = {e["verdict"] for e in recs[0]["evaluations"]}
            # ETH は保有ナシなので regime_blocked、BTC は already_held
            self.assertTrue(any("regime_blocked" in v for v in verdicts))

    # ------------------------------------------------------------------
    # STOP ファイルがあると buys=0、保有評価は継続
    # ------------------------------------------------------------------
    def test_stop_file_suppresses_buys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            stop = sd / "STOP_TEST"
            stop.write_text("", encoding="utf-8")
            cfg = {**CFG_BTC_ETH, "risk": {**CFG_BTC_ETH["risk"], "stop_file": str(stop)}}
            state, w, sc, g, ex, n = self._components(sd, cfg)
            run_cycle(cfg, state, w, sc, g, ex, n, regime_gate=None)
            recs = _read_cycle_log(sd)
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0]["stop_file"])
            sides = [d["side"] for d in recs[0]["decisions"]]
            self.assertNotIn("buy", sides)

    # ------------------------------------------------------------------
    # HALT 中は cycle スキップ
    # ------------------------------------------------------------------
    def test_halt_skips_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            state, w, sc, g, ex, n = self._components(sd)
            state.set_halt("test_halt")
            run_cycle(CFG_BTC_ETH, state, w, sc, g, ex, n, regime_gate=None)
            recs = _read_cycle_log(sd)
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0]["halted"])
            self.assertEqual(recs[0]["evaluations"], [])
            self.assertEqual(recs[0]["decisions"], [])

    # ------------------------------------------------------------------
    # 既保有 + 損切り価格 → sell decision が記録される
    # ------------------------------------------------------------------
    def test_stop_loss_triggers_sell_decision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            state, w, sc, g, ex, n = self._components(
                sd, source=_StaticPriceSource({"BTC_JPY": 9_000_000.0}),  # -10%
            )
            state.set_position(Position("BTC_JPY", 10_000.0, 10_000_000.0, 0.0))
            run_cycle(CFG_BTC_ETH, state, w, sc, g, ex, n, regime_gate=None)
            recs = _read_cycle_log(sd)
            self.assertEqual(len(recs), 1)
            sells = [d for d in recs[0]["decisions"] if d["side"] == "sell"]
            self.assertEqual(len(sells), 1)
            self.assertIn("stop_loss", sells[0]["reason"])
            # sell 後はポジション消えてる
            self.assertNotIn("BTC_JPY", state.positions())

    # ------------------------------------------------------------------
    # max_positions=1 で 2 銘柄候補なら 1 つだけ buy
    # ------------------------------------------------------------------
    def test_max_positions_limits_concurrent_buys(self) -> None:
        # max_orders_per_cycle=2 でも max_positions=1 で 1 件に絞られる
        cfg = {
            **CFG_BTC_ETH,
            "loop": {**CFG_BTC_ETH["loop"], "max_orders_per_cycle": 2},
            "limits": {**CFG_BTC_ETH["limits"], "max_positions": 1},
        }
        with tempfile.TemporaryDirectory() as td:
            sd = Path(td)
            state, w, sc, g, ex, n = self._components(sd, cfg)
            run_cycle(cfg, state, w, sc, g, ex, n, regime_gate=None)
            recs = _read_cycle_log(sd)
            buys = [d for d in recs[0]["decisions"] if d["side"] == "buy"]
            self.assertLessEqual(len(buys), 1)


if __name__ == "__main__":
    unittest.main()
