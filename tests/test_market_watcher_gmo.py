"""GmoMarketDataSource のテスト。

GmoApiClient を FakeClient に差し替え、シンボルマッピング・データ変換・
interval 選択を検証する。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from market_watcher import (  # noqa: E402
    GmoMarketDataSource,
    _pick_interval,
    _to_gmo_symbol,
    _ymd_utc,
)


class _FakeClient:
    """get_ticker / get_klines の呼び出しを記録し、固定レスポンスを返す。"""

    def __init__(self, ticker_response=None, klines_response=None) -> None:
        self.ticker_calls: list[str] = []
        self.klines_calls: list[tuple[str, str, str]] = []
        self._ticker_response = ticker_response or {
            "status": 0,
            "data": [{
                "symbol": "BTC",
                "last": "10000000",
                "bid": "9999000",
                "ask": "10001000",
                "volume": "12.34",
            }],
        }
        self._klines_response = klines_response or {
            "status": 0,
            "data": [
                {
                    "openTime": str((1700000000 + i * 300) * 1000),
                    "open": str(100 + i),
                    "high": str(101 + i),
                    "low": str(99 + i),
                    "close": str(100 + i),
                    "volume": str(1.0 + i),
                }
                for i in range(10)
            ],
        }

    def get_ticker(self, symbol: str) -> dict:
        self.ticker_calls.append(symbol)
        return self._ticker_response

    def get_klines(self, symbol: str, interval: str, date: str) -> dict:
        self.klines_calls.append((symbol, interval, date))
        return self._klines_response


# ----------------------------------------------------------------------
# シンボルマッピング
# ----------------------------------------------------------------------
class TestSymbolMapping(unittest.TestCase):
    def test_btc_jpy_to_btc(self) -> None:
        self.assertEqual(_to_gmo_symbol("BTC_JPY"), "BTC")

    def test_eth_jpy_to_eth(self) -> None:
        self.assertEqual(_to_gmo_symbol("ETH_JPY"), "ETH")

    def test_sol_xrp_doge(self) -> None:
        self.assertEqual(_to_gmo_symbol("SOL_JPY"), "SOL")
        self.assertEqual(_to_gmo_symbol("XRP_JPY"), "XRP")
        self.assertEqual(_to_gmo_symbol("DOGE_JPY"), "DOGE")

    def test_unknown_passthrough(self) -> None:
        self.assertEqual(_to_gmo_symbol("UNKNOWN"), "UNKNOWN")


# ----------------------------------------------------------------------
# interval の選び方
# ----------------------------------------------------------------------
class TestPickInterval(unittest.TestCase):
    def test_short_n_uses_5min(self) -> None:
        self.assertEqual(_pick_interval(30), "5min")
        self.assertEqual(_pick_interval(288), "5min")

    def test_long_n_uses_1hour(self) -> None:
        self.assertEqual(_pick_interval(289), "1hour")
        self.assertEqual(_pick_interval(500), "1hour")


# ----------------------------------------------------------------------
# date 文字列
# ----------------------------------------------------------------------
class TestYmdUtc(unittest.TestCase):
    def test_format_is_yyyymmdd(self) -> None:
        # 2026-05-18 12:00 UTC = epoch 1779105600
        self.assertEqual(_ymd_utc(1779105600.0), "20260518")


# ----------------------------------------------------------------------
# fetch_tickers
# ----------------------------------------------------------------------
class TestFetchTickers(unittest.TestCase):
    def test_passes_mapped_symbol_to_client(self) -> None:
        client = _FakeClient()
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        src.fetch_tickers(["BTC_JPY"])
        self.assertEqual(client.ticker_calls, ["BTC"])

    def test_returns_ticker_with_bot_symbol(self) -> None:
        client = _FakeClient()
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        tickers = src.fetch_tickers(["BTC_JPY"])
        self.assertIn("BTC_JPY", tickers)
        t = tickers["BTC_JPY"]
        self.assertEqual(t.symbol, "BTC_JPY")
        self.assertEqual(t.last, 10000000.0)
        self.assertEqual(t.bid, 9999000.0)
        self.assertEqual(t.ask, 10001000.0)
        self.assertEqual(t.volume, 12.34)
        self.assertEqual(t.ts, 1700000000.0)

    def test_multiple_symbols(self) -> None:
        client = _FakeClient()
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        tickers = src.fetch_tickers(["BTC_JPY", "ETH_JPY"])
        self.assertEqual(client.ticker_calls, ["BTC", "ETH"])
        self.assertEqual(set(tickers.keys()), {"BTC_JPY", "ETH_JPY"})

    def test_missing_fields_fall_back(self) -> None:
        client = _FakeClient(
            ticker_response={"status": 0, "data": [{"symbol": "BTC", "last": "5"}]}
        )
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        t = src.fetch_tickers(["BTC_JPY"])["BTC_JPY"]
        # bid/ask が無ければ last にフォールバック
        self.assertEqual(t.last, 5.0)
        self.assertEqual(t.bid, 5.0)
        self.assertEqual(t.ask, 5.0)


# ----------------------------------------------------------------------
# fetch_ohlcv
# ----------------------------------------------------------------------
class TestFetchOhlcv(unittest.TestCase):
    def test_uses_5min_for_short_window(self) -> None:
        client = _FakeClient()
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        src.fetch_ohlcv(["BTC_JPY"], n=30)
        self.assertEqual(len(client.klines_calls), 1)
        sym, interval, date = client.klines_calls[0]
        self.assertEqual(sym, "BTC")
        self.assertEqual(interval, "5min")
        # 2023-11-14 22:13:20 UTC
        self.assertEqual(date, "20231114")

    def test_uses_1hour_when_n_exceeds_5min_capacity(self) -> None:
        client = _FakeClient()
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        src.fetch_ohlcv(["BTC_JPY"], n=500)
        _sym, interval, _date = client.klines_calls[0]
        self.assertEqual(interval, "1hour")

    def test_returns_candles_with_required_fields(self) -> None:
        client = _FakeClient()
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        result = src.fetch_ohlcv(["BTC_JPY"], n=30)
        candles = result["BTC_JPY"]
        self.assertEqual(len(candles), 10)
        c0 = candles[0]
        # openTime "1700000000000" ms → 1700000000 s
        self.assertEqual(c0.ts, 1700000000.0)
        self.assertEqual(c0.open, 100.0)
        self.assertEqual(c0.high, 101.0)
        self.assertEqual(c0.low, 99.0)
        self.assertEqual(c0.close, 100.0)

    def test_slices_to_last_n_when_longer(self) -> None:
        client = _FakeClient()
        src = GmoMarketDataSource(client, clock_fn=lambda: 1700000000.0)
        result = src.fetch_ohlcv(["BTC_JPY"], n=3)
        candles = result["BTC_JPY"]
        self.assertEqual(len(candles), 3)
        # 末尾 3 本 = i=7,8,9 → close=107,108,109
        closes = [c.close for c in candles]
        self.assertEqual(closes, [107.0, 108.0, 109.0])


if __name__ == "__main__":
    unittest.main()
