"""apply_portfolio_constraints の分岐網羅。

max_positions / core/sat ratio cap / cash_ratio block / max_orders /
unknown_group / no_ticker / strong>normal の優先順を確認。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_watcher import MarketSnapshot, Ticker  # noqa: E402
from risk_guard import RiskGuard  # noqa: E402
from scorer import Score  # noqa: E402
from state_store import Position, StateStore  # noqa: E402


# 1サイクル最大2件、ratio cap で挙動を見やすい数値に固定
BASE_CFG: dict = {
    "symbols": {
        "core": ["BTC_JPY", "ETH_JPY"],
        "satellite": ["SOL_JPY"],
    },
    "limits": {
        "max_positions": 2,
        "min_cash_ratio": 0.20,
        "max_core_ratio": 0.35,
        "max_sat_ratio": 0.20,
    },
    "risk": {
        "halt_on_error": True,
        "max_consecutive_errors": 5,
        "halt_on_price_gap_pct": 10.0,
        "per_trade_jpy_max": 100_000,
        "block_buy_below_cash_ratio": 0.20,
        "stop_file": "./STOP_TEST",
    },
    "scorer": {
        "thresholds": {
            "buy_candidate": {"total": 70, "trend": 18, "liquidity": 10, "heat": -8},
            "strong_buy": {"total": 78, "trend": 22, "liquidity": 12, "heat": -5},
            "dup_penalty_block": -8,
        },
    },
    "exits": {"stop_loss_pct": -4.0, "take_profit_pct": 6.0, "cooldown_min": 180},
    "loop": {"max_orders_per_cycle": 2},
    "portfolio": {"initial_cash_jpy": 1_000_000},
}


def _ticker(sym: str, price: float) -> Ticker:
    return Ticker(symbol=sym, last=price, bid=price - 1, ask=price + 1, volume=1.0, ts=0.0)


def _snap(*pairs: tuple[str, float]) -> MarketSnapshot:
    return MarketSnapshot(ts=0.0, tickers={s: _ticker(s, p) for s, p in pairs})


def _strong_score(sym: str, total: float = 100.0) -> Score:
    return Score(
        symbol=sym, total=total, trend=25.0, liquidity=15.0, heat=0.0,
        dup_penalty=0.0, rule_score=total, ai_score=0.0,
    )


class TestPortfolioConstraints(unittest.TestCase):
    def _guard(self, td: str, cfg: dict | None = None) -> RiskGuard:
        state = StateStore(path=str(Path(td) / "state.json"))
        return RiskGuard(cfg or BASE_CFG, state)

    # ------------------------------------------------------------------
    # max_positions
    # ------------------------------------------------------------------
    def test_max_positions_blocks_when_already_held(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state = StateStore(path=str(Path(td) / "state.json"))
            # 既に2銘柄保有 → max_positions=2 に達してる
            state.set_position(Position("BTC_JPY", 100_000, 5_000_000.0, 0.0))
            state.set_position(Position("SOL_JPY", 100_000, 20_000.0, 0.0))
            g = RiskGuard(BASE_CFG, state)
            decisions, rej = g.apply_portfolio_constraints(
                [_strong_score("ETH_JPY")],
                _snap(("ETH_JPY", 500_000.0)),
                cash_jpy=800_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(decisions, [])
            self.assertIn("max_positions", rej.get("ETH_JPY", ""))

    def test_max_orders_per_cycle_caps_decisions(self) -> None:
        cfg = {**BASE_CFG, "loop": {"max_orders_per_cycle": 1}}
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td, cfg)
            decisions, rej = g.apply_portfolio_constraints(
                [_strong_score("BTC_JPY"), _strong_score("ETH_JPY")],
                _snap(("BTC_JPY", 10_000_000), ("ETH_JPY", 500_000)),
                cash_jpy=1_000_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(len(decisions), 1)
            # 後から rejected の理由に max_orders が入る
            rejected = next(s for s in ("BTC_JPY", "ETH_JPY") if s not in {d.symbol for d in decisions})
            self.assertIn("max_orders_per_cycle", rej.get(rejected, ""))

    # ------------------------------------------------------------------
    # ratio cap
    # ------------------------------------------------------------------
    def test_core_ratio_cap_clips_size(self) -> None:
        # equity 1M、max_core_ratio=0.35、per_trade_jpy_max=100k → cap=350k は引っかからず
        # max_core_ratio=0.05 にすると cap=50k で per_trade_jpy_max(100k) を上回らないので clip
        cfg = {**BASE_CFG, "limits": {**BASE_CFG["limits"], "max_core_ratio": 0.05}}
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td, cfg)
            decisions, _ = g.apply_portfolio_constraints(
                [_strong_score("BTC_JPY")],
                _snap(("BTC_JPY", 10_000_000)),
                cash_jpy=1_000_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(len(decisions), 1)
            # 100k → 50k に clip されるはず
            self.assertEqual(decisions[0].size_jpy, 50_000)

    def test_satellite_ratio_cap_clips_size(self) -> None:
        cfg = {**BASE_CFG, "limits": {**BASE_CFG["limits"], "max_sat_ratio": 0.03}}
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td, cfg)
            decisions, _ = g.apply_portfolio_constraints(
                [_strong_score("SOL_JPY")],
                _snap(("SOL_JPY", 20_000)),
                cash_jpy=1_000_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].size_jpy, 30_000)

    # ------------------------------------------------------------------
    # cash ratio block
    # ------------------------------------------------------------------
    def test_post_buy_cash_ratio_block(self) -> None:
        # cash_ratio が下限 20% を割る注文は出さない
        # cash=300k / equity=1M で post_cash=300k-100k=200k、ratio=0.20 → ギリ通過
        # block_buy_below_cash_ratio=0.30 なら post_ratio<0.30 でブロック
        cfg = {**BASE_CFG, "risk": {**BASE_CFG["risk"], "block_buy_below_cash_ratio": 0.30}}
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td, cfg)
            decisions, rej = g.apply_portfolio_constraints(
                [_strong_score("BTC_JPY")],
                _snap(("BTC_JPY", 10_000_000)),
                cash_jpy=300_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(decisions, [])
            self.assertIn("cash_ratio", rej.get("BTC_JPY", ""))

    # ------------------------------------------------------------------
    # symbol group
    # ------------------------------------------------------------------
    def test_unknown_group_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td)
            # XRP_JPY は core/sat どちらにも未登録
            decisions, rej = g.apply_portfolio_constraints(
                [_strong_score("XRP_JPY")],
                _snap(("XRP_JPY", 100.0)),
                cash_jpy=1_000_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(decisions, [])
            self.assertEqual(rej.get("XRP_JPY"), "unknown_group")

    def test_no_ticker_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td)
            decisions, rej = g.apply_portfolio_constraints(
                [_strong_score("BTC_JPY")],
                _snap(("ETH_JPY", 500_000)),  # BTC の ticker が無い
                cash_jpy=1_000_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(decisions, [])
            self.assertEqual(rej.get("BTC_JPY"), "no_ticker")

    # ------------------------------------------------------------------
    # 優先順
    # ------------------------------------------------------------------
    def test_strong_buy_prioritized_over_normal(self) -> None:
        # max_orders=1 で strong と normal を混ぜる → strong が選ばれる
        cfg = {**BASE_CFG, "loop": {"max_orders_per_cycle": 1}}
        normal = Score(
            symbol="BTC_JPY", total=72.0, trend=20.0, liquidity=11.0, heat=0.0,
            dup_penalty=0.0, rule_score=72.0, ai_score=0.0,
        )  # buy だが strong 閾値未満
        strong = _strong_score("ETH_JPY")  # 100 / 25 / 15 → strong
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td, cfg)
            decisions, _ = g.apply_portfolio_constraints(
                [normal, strong],
                _snap(("BTC_JPY", 10_000_000), ("ETH_JPY", 500_000)),
                cash_jpy=1_000_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(len(decisions), 1)
            self.assertEqual(decisions[0].symbol, "ETH_JPY")
            self.assertTrue(decisions[0].strong)

    def test_size_consumes_remaining_cash(self) -> None:
        # 1注文目で per_trade_jpy_max=100k 使う → remaining=50k → 2注文目は 50k
        # cash_ratio block は今回の検証対象外なので 0 に下げて切り離す。
        cfg = {
            **BASE_CFG,
            "loop": {"max_orders_per_cycle": 2},
            "risk": {**BASE_CFG["risk"], "per_trade_jpy_max": 100_000,
                     "block_buy_below_cash_ratio": 0.0},
        }
        with tempfile.TemporaryDirectory() as td:
            g = self._guard(td, cfg)
            decisions, _ = g.apply_portfolio_constraints(
                [_strong_score("BTC_JPY", total=110), _strong_score("ETH_JPY", total=100)],
                _snap(("BTC_JPY", 10_000_000), ("ETH_JPY", 500_000)),
                cash_jpy=150_000, total_equity_jpy=1_000_000,
            )
            self.assertEqual(len(decisions), 2)
            self.assertEqual(decisions[0].size_jpy, 100_000)
            self.assertEqual(decisions[1].size_jpy, 50_000)


if __name__ == "__main__":
    unittest.main()
