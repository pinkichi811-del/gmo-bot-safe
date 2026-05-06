"""scripts/aggregate.py の動作確認。

Phase 2 完了判定（観察ログ集計）の信頼性を担保する。
subprocess で実スクリプトを起動し、stdout の指標が期待通り集計されるか確認。
PnL/PF/DD/regime/cash_ratio の解析関数は直接 import して単体テストする。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "aggregate.py"

# aggregate.py の内部関数を import するためにパスを通す
sys.path.insert(0, str(ROOT / "scripts"))
import aggregate  # noqa: E402


def _make_record(
    iso_ts: str,
    halted: bool = False,
    stop_file: bool = False,
    evaluations: list[dict] | None = None,
    decisions: list[dict] | None = None,
    errors: list[str] | None = None,
) -> dict:
    return {
        "cycle_ts": 0.0,
        "iso_ts": iso_ts,
        "halted": halted,
        "halt_reason": "",
        "stop_file": stop_file,
        "portfolio": {},
        "evaluations": evaluations or [],
        "decisions": decisions or [],
        "errors": errors or [],
    }


def _make_eval(
    symbol: str, total: float = 80.0, trend: float = 20.0,
    verdict: str = "selected", buy_candidate: bool = True,
    strong_buy: bool = False,
) -> dict:
    return {
        "symbol": symbol,
        "trend": trend,
        "liquidity": 10.0,
        "heat": 0.0,
        "volatility": 0.0,
        "dup_penalty": 0.0,
        "cash_bonus": 0.0,
        "rule": total,
        "ai": 0.0,
        "total": total,
        "buy_candidate": buy_candidate,
        "strong_buy": strong_buy,
        "verdict": verdict,
    }


def _run(state_dir: Path, *args: str) -> subprocess.CompletedProcess:
    # Windows では子プロセスの stdout/stderr が cp932 になりがちで、
    # capture_output と text=True の組合せだと日本語混じりの decode で
    # subprocess が None を返すケースがある。子プロセス側で UTF-8 を強制。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--state-dir", str(state_dir), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )


class TestAggregate(unittest.TestCase):
    def test_no_score_log_dir_returns_error(self) -> None:
        """score_log/ が無い → 親切なメッセージで return 1。"""
        with tempfile.TemporaryDirectory() as td:
            res = _run(Path(td))
            self.assertEqual(res.returncode, 1)
            self.assertIn("score_log", res.stderr)

    def test_empty_log_runs_clean(self) -> None:
        """score_log/ あるが空 → return 0 で cycles=0。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "score_log").mkdir()
            res = _run(Path(td))
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("cycles=0", res.stdout)

    def test_counts_verdicts_and_decisions(self) -> None:
        """1 ファイルに複数 record を書いて集計が一致するか。"""
        with tempfile.TemporaryDirectory() as td:
            today = datetime.now(timezone.utc).date().isoformat()
            log_dir = Path(td) / "score_log"
            log_dir.mkdir()
            log = log_dir / f"{today}.jsonl"

            records = [
                _make_record(
                    f"{today}T00:00:00Z",
                    evaluations=[_make_eval("BTC_JPY", verdict="selected"),
                                 _make_eval("ETH_JPY", verdict="below_threshold:trend",
                                            buy_candidate=False)],
                    decisions=[{"symbol": "BTC_JPY", "side": "buy",
                                "size_jpy": 10_000, "price_ref": 1.0,
                                "reason": "buy", "strong": False}],
                ),
                _make_record(
                    f"{today}T00:05:00Z",
                    evaluations=[_make_eval("BTC_JPY", verdict="already_held",
                                            buy_candidate=False),
                                 _make_eval("ETH_JPY", verdict="selected",
                                            strong_buy=True)],
                    decisions=[{"symbol": "BTC_JPY", "side": "sell",
                                "size_jpy": 10_000, "price_ref": 1.0,
                                "reason": "stop_loss", "strong": False}],
                ),
                _make_record(f"{today}T00:10:00Z", halted=True,
                             errors=["fetch: timeout"]),
            ]
            log.write_text(
                "\n".join(json.dumps(r) for r in records) + "\n",
                encoding="utf-8",
            )

            res = _run(Path(td))
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            out = res.stdout
            self.assertIn("cycles=3", out)
            self.assertIn("halted=1", out)
            # verdict 分布
            self.assertIn("selected", out)
            self.assertIn("already_held", out)
            self.assertIn("below_threshold:trend", out)
            # symbol 別 buy/sell カウント
            self.assertIn("BTC_JPY", out)
            self.assertIn("buy=1", out)
            self.assertIn("sell=1", out)
            # errors 集計
            self.assertIn("fetch: timeout", out)

    def test_skips_invalid_json_lines(self) -> None:
        """壊れた行があってもスキップして集計続行する。"""
        with tempfile.TemporaryDirectory() as td:
            today = datetime.now(timezone.utc).date().isoformat()
            log_dir = Path(td) / "score_log"
            log_dir.mkdir()
            log = log_dir / f"{today}.jsonl"
            valid = _make_record(f"{today}T00:00:00Z")
            log.write_text(
                "this is not json\n"
                + json.dumps(valid) + "\n"
                + "{broken json\n",
                encoding="utf-8",
            )
            res = _run(Path(td))
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("cycles=1", res.stdout)

    def test_days_argument_reads_multiple_files(self) -> None:
        """--days 3 で 3 日分の jsonl を集計する。"""
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "score_log"
            log_dir.mkdir()
            today = datetime.now(timezone.utc).date()
            from datetime import timedelta
            for i in range(3):
                d = today - timedelta(days=i)
                log = log_dir / f"{d.isoformat()}.jsonl"
                log.write_text(
                    json.dumps(_make_record(f"{d.isoformat()}T00:00:00Z")) + "\n",
                    encoding="utf-8",
                )
            res = _run(Path(td), "--days", "3")
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("cycles=3", res.stdout)


class TestParseRealizedPct(unittest.TestCase):
    def test_stop_loss(self) -> None:
        self.assertEqual(aggregate.parse_realized_pct("stop_loss -2.50%"), -2.50)

    def test_take_profit(self) -> None:
        self.assertEqual(aggregate.parse_realized_pct("take_profit +8.31%"), 8.31)

    def test_trail(self) -> None:
        self.assertEqual(
            aggregate.parse_realized_pct("trail -3.05% from peak"), -3.05,
        )

    def test_max_hold(self) -> None:
        self.assertEqual(
            aggregate.parse_realized_pct("max_hold 300bars pnl=+4.88%"), 4.88,
        )

    def test_buy_returns_none(self) -> None:
        self.assertIsNone(
            aggregate.parse_realized_pct("buy total=96.7 trend=19.2 liq=10.3"),
        )

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(aggregate.parse_realized_pct(""))


class TestIsFakeLoss(unittest.TestCase):
    def test_deep_stop_loss_is_fake(self) -> None:
        # 再起動跨ぎ偽損失の代表例 (data/dry_run_orders.csv:2 の実データより)
        self.assertTrue(
            aggregate.is_fake_loss("sell", "stop_loss -48.20%", -30.0),
        )

    def test_normal_stop_loss_is_real(self) -> None:
        self.assertFalse(
            aggregate.is_fake_loss("sell", "stop_loss -2.50%", -30.0),
        )

    def test_take_profit_is_never_fake(self) -> None:
        self.assertFalse(
            aggregate.is_fake_loss("sell", "take_profit +8.31%", -30.0),
        )

    def test_buy_is_never_fake(self) -> None:
        self.assertFalse(
            aggregate.is_fake_loss("buy", "buy total=96.7", -30.0),
        )


class TestComputePnlStats(unittest.TestCase):
    @staticmethod
    def _order(iso_ts: str, side: str, size_jpy: float, reason: str) -> dict:
        return {"iso_ts": iso_ts, "side": side,
                "size_jpy": str(size_jpy), "reason": reason,
                "symbol": "BTC_JPY"}

    def test_basic_round_trip(self) -> None:
        # 10000 JPY × +5% = +500 JPY
        orders = [
            self._order("2026-04-13T00:00:00Z", "buy", 10000, "buy ..."),
            self._order("2026-04-13T01:00:00Z", "sell", 10000, "take_profit +5.00%"),
        ]
        s = aggregate.compute_pnl_stats(orders, 1_000_000, -30.0)
        self.assertEqual(s["trades"], 1)
        self.assertEqual(s["wins"], 1)
        self.assertEqual(s["losses"], 0)
        self.assertAlmostEqual(s["pnl_jpy"], 500.0, places=2)
        self.assertAlmostEqual(s["pnl_pct"], 0.05, places=4)

    def test_excludes_fake_losses(self) -> None:
        orders = [
            self._order("2026-04-13T00:00:00Z", "sell", 10000, "stop_loss -48.20%"),
            self._order("2026-04-13T01:00:00Z", "sell", 10000, "stop_loss -2.00%"),
            self._order("2026-04-13T02:00:00Z", "sell", 10000, "take_profit +5.00%"),
        ]
        s = aggregate.compute_pnl_stats(orders, 1_000_000, -30.0)
        # 偽損失は除外され、残り 2 件
        self.assertEqual(s["trades"], 2)
        self.assertEqual(s["fake_excluded"], 1)
        # PnL = -200 + 500 = +300
        self.assertAlmostEqual(s["pnl_jpy"], 300.0, places=2)

    def test_profit_factor_and_max_dd(self) -> None:
        # 系列: +1000, -500, +300, -800 (size=10000で +10%, -5%, +3%, -8%)
        # 累積: 1000, 500, 800, 0  → peak=1000, 谷=0 → DD=1000
        orders = [
            self._order("2026-04-13T00:00:00Z", "sell", 10000, "take_profit +10.00%"),
            self._order("2026-04-13T01:00:00Z", "sell", 10000, "stop_loss -5.00%"),
            self._order("2026-04-13T02:00:00Z", "sell", 10000, "take_profit +3.00%"),
            self._order("2026-04-13T03:00:00Z", "sell", 10000, "stop_loss -8.00%"),
        ]
        s = aggregate.compute_pnl_stats(orders, 1_000_000, -30.0)
        self.assertEqual(s["trades"], 4)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 2)
        # PF = (1000+300) / (500+800) = 1300/1300 = 1.0
        self.assertAlmostEqual(s["profit_factor"], 1.0, places=4)
        # max DD = 1000 JPY (1000 peak → 0 trough)
        self.assertAlmostEqual(s["max_dd_jpy"], 1000.0, places=2)
        self.assertAlmostEqual(s["max_dd_pct"], 0.10, places=4)

    def test_no_orders_returns_zero(self) -> None:
        s = aggregate.compute_pnl_stats([], 1_000_000, -30.0)
        self.assertEqual(s["trades"], 0)
        self.assertEqual(s["pnl_jpy"], 0.0)


class TestComputeRegimeStats(unittest.TestCase):
    def test_basic_block_rate(self) -> None:
        records = [
            {"regime": {"allow_buy": True}},
            {"regime": {"allow_buy": False}},
            {"regime": {"allow_buy": False}},
            {"regime": {"allow_buy": True}},
        ]
        s = aggregate.compute_regime_stats(records)
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["blocked"], 2)
        self.assertAlmostEqual(s["block_rate_pct"], 50.0)

    def test_records_without_regime_excluded(self) -> None:
        # regime キー無しのレコードは母数に入らない
        records = [
            {"regime": {"allow_buy": False}},
            {},  # 旧 champion (regime_filter なし) のレコード
            {"halted": True},
        ]
        s = aggregate.compute_regime_stats(records)
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["blocked"], 1)

    def test_no_regime_records(self) -> None:
        s = aggregate.compute_regime_stats([{}, {"halted": True}])
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["block_rate_pct"], 0.0)


class TestComputeCashRatioStats(unittest.TestCase):
    def test_basic(self) -> None:
        records = [
            {"portfolio": {"cash_ratio": 1.0}},
            {"portfolio": {"cash_ratio": 0.5}},
            {"portfolio": {"cash_ratio": 0.18}},  # 20% を割っている
        ]
        s = aggregate.compute_cash_ratio_stats(records, min_threshold=0.20)
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["min"], 0.18)
        self.assertAlmostEqual(s["mean"], (1.0 + 0.5 + 0.18) / 3)
        self.assertEqual(s["below_count"], 1)

    def test_records_without_portfolio_excluded(self) -> None:
        records = [{"portfolio": {"cash_ratio": 0.8}}, {}, {"halted": True}]
        s = aggregate.compute_cash_ratio_stats(records)
        self.assertEqual(s["n"], 1)


class TestAggregateWithOrdersCsv(unittest.TestCase):
    """subprocess で aggregate.py を呼び、新セクションの出力を確認する。"""

    @staticmethod
    def _write_orders(csv_path: Path, rows: list[dict]) -> None:
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            import csv as csvmod
            w = csvmod.writer(f)
            w.writerow(["ts", "iso_ts", "mode", "symbol", "side",
                        "size_jpy", "price_ref", "reason", "strong"])
            for r in rows:
                w.writerow([0.0, r["iso_ts"], "dry_run", r.get("symbol", "BTC_JPY"),
                            r["side"], r["size_jpy"], 1.0, r["reason"], False])

    def test_pnl_section_appears(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            today = datetime.now(timezone.utc).date().isoformat()
            base = Path(td)
            (base / "score_log").mkdir()
            (base / "score_log" / f"{today}.jsonl").write_text(
                json.dumps(_make_record(f"{today}T00:00:00Z")) + "\n",
                encoding="utf-8",
            )
            self._write_orders(base / "dry_run_orders.csv", [
                {"iso_ts": f"{today}T00:00:00Z", "side": "buy",
                 "size_jpy": 10000, "reason": "buy ..."},
                {"iso_ts": f"{today}T01:00:00Z", "side": "sell",
                 "size_jpy": 10000, "reason": "take_profit +5.00%"},
                {"iso_ts": f"{today}T02:00:00Z", "side": "sell",
                 "size_jpy": 10000, "reason": "stop_loss -48.20%"},
            ])
            res = _run(base, "--initial-cash", "1000000")
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("pnl summary", res.stdout)
            self.assertIn("excluded=1", res.stdout)
            self.assertIn("trades=1 wins=1", res.stdout)

    def test_regime_section_when_records_present(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            today = datetime.now(timezone.utc).date().isoformat()
            base = Path(td)
            (base / "score_log").mkdir()
            rec1 = _make_record(f"{today}T00:00:00Z")
            rec1["regime"] = {"allow_buy": True, "reason": ""}
            rec2 = _make_record(f"{today}T00:05:00Z")
            rec2["regime"] = {"allow_buy": False, "reason": "regime_blocked:ndx_trend"}
            (base / "score_log" / f"{today}.jsonl").write_text(
                json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n",
                encoding="utf-8",
            )
            res = _run(base)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("regime gate", res.stdout)
            self.assertIn("blocks=1", res.stdout)
            self.assertIn("total=2", res.stdout)

    def test_cash_ratio_section(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            today = datetime.now(timezone.utc).date().isoformat()
            base = Path(td)
            (base / "score_log").mkdir()
            rec = _make_record(f"{today}T00:00:00Z")
            rec["portfolio"] = {"cash_ratio": 0.18}
            (base / "score_log" / f"{today}.jsonl").write_text(
                json.dumps(rec) + "\n",
                encoding="utf-8",
            )
            res = _run(base)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("cash_ratio", res.stdout)
            self.assertIn("below_0.20=1", res.stdout)


if __name__ == "__main__":
    unittest.main()
