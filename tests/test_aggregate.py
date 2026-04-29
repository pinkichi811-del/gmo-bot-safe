"""scripts/aggregate.py の動作確認。

Phase 2 完了判定（観察ログ集計）の信頼性を担保する。
subprocess で実スクリプトを起動し、stdout の指標が期待通り集計されるか確認。
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


if __name__ == "__main__":
    unittest.main()
