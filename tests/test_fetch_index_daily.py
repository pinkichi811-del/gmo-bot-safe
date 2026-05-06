"""scripts/fetch_index_daily.py の動作確認。

外部 API (Yahoo Finance) は叩かず、parse / merge / write の純関数だけ単体テストする。
"""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fetch_index_daily as fid  # noqa: E402


# Yahoo Finance v8 chart API のレスポンス例 (NDX 3 日分)
# ts は market open (UTC 14:30 / 13:30) — 既存 NDX_d.csv の format と整合
SAMPLE_YAHOO_PAYLOAD = {
    "chart": {
        "result": [{
            "timestamp": [
                1774445400,  # 2026-03-25 14:30 UTC
                1774531800,  # 2026-03-26
                1774963800,  # 2026-03-31
            ],
            "indicators": {
                "quote": [{
                    "open":  [24236.40, 23913.19, 23208.00],
                    "high":  [24314.25, 24029.51, 23789.60],
                    "low":   [24081.38, 23574.72, 23198.64],
                    "close": [24162.98, 23586.99, 23740.19],
                    "volume": [0, 0, 0],
                }],
            },
        }],
        "error": None,
    },
}


class TestParseYahooChart(unittest.TestCase):
    def test_basic(self):
        bars = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[0]["date"], "2026-03-25")
        self.assertEqual(bars[0]["open"], 24236.40)
        self.assertEqual(bars[-1]["close"], 23740.19)

    def test_ts_passes_through(self):
        """Yahoo の ts はそのまま保持される (既存 CSV と同じ market open ベース)。"""
        bars = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
        self.assertEqual(bars[0]["ts"], 1774445400)
        self.assertEqual(bars[-1]["ts"], 1774963800)

    def test_start_filter(self):
        bars = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD, start=date(2026, 3, 26))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["date"], "2026-03-26")

    def test_skip_null_rows(self):
        payload = {
            "chart": {
                "result": [{
                    "timestamp": [1774445400, 1774531800, 1774963800],
                    "indicators": {
                        "quote": [{
                            "open":  [24236.40, None, 23208.00],
                            "high":  [24314.25, None, 23789.60],
                            "low":   [24081.38, None, 23198.64],
                            "close": [24162.98, None, 23740.19],
                        }],
                    },
                }],
                "error": None,
            },
        }
        bars = fid.parse_yahoo_chart(payload)
        self.assertEqual(len(bars), 2)
        self.assertEqual([b["date"] for b in bars], ["2026-03-25", "2026-03-31"])

    def test_empty_result(self):
        bars = fid.parse_yahoo_chart({"chart": {"result": [], "error": None}})
        self.assertEqual(bars, [])

    def test_error_payload_raises(self):
        with self.assertRaises(RuntimeError):
            fid.parse_yahoo_chart({"chart": {"error": {"code": "Not Found"}}})


class TestMergeWithExisting(unittest.TestCase):
    def _write(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "date", "open", "high", "low", "close"])
            for r in rows:
                w.writerow([r["ts"], r["date"], r["open"], r["high"], r["low"], r["close"]])

    def test_merge_no_existing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "X_d.csv"
            new = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
            merged = fid.merge_with_existing(new, path)
            self.assertEqual(len(merged), 3)
            self.assertEqual(merged[0]["date"], "2026-03-25")

    def test_merge_keeps_old_and_adds_new(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "X_d.csv"
            self._write(path, [{
                "ts": 1700000000, "date": "2024-01-01",
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
            }])
            new = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
            merged = fid.merge_with_existing(new, path)
            self.assertEqual(len(merged), 4)
            self.assertEqual(merged[0]["date"], "2024-01-01")
            self.assertEqual(merged[-1]["date"], "2026-03-31")

    def test_merge_overwrites_same_date(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "X_d.csv"
            self._write(path, [{
                "ts": 0, "date": "2026-03-25",
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 99.99,
            }])
            new = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
            merged = fid.merge_with_existing(new, path)
            self.assertEqual(len(merged), 3)
            mar25 = next(b for b in merged if b["date"] == "2026-03-25")
            self.assertEqual(mar25["close"], 24162.98)

    def test_sorted_by_ts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "X_d.csv"
            new = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
            merged = fid.merge_with_existing(new, path)
            ts_list = [b["ts"] for b in merged]
            self.assertEqual(ts_list, sorted(ts_list))


class TestWriteCsv(unittest.TestCase):
    def test_write_and_reread_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "X_d.csv"
            new = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
            fid.write_csv(new, path)
            self.assertTrue(path.exists())
            with path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["date"], "2026-03-25")
            self.assertEqual(set(rows[0].keys()), {"ts", "date", "open", "high", "low", "close"})

    def test_write_format_matches_existing_ndx_csv(self):
        """既存 data/market/NDX_d.csv と同じヘッダ順であること。"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "X_d.csv"
            new = fid.parse_yahoo_chart(SAMPLE_YAHOO_PAYLOAD)
            fid.write_csv(new, path)
            with path.open(encoding="utf-8") as f:
                header = f.readline().strip()
            self.assertEqual(header, "ts,date,open,high,low,close")


class TestCliEntry(unittest.TestCase):
    def test_unknown_index_returns_error(self):
        rc = fid.main(["--indices", "BOGUS", "--dry-run"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
