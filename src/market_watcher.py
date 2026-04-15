"""市場データ取得。

抽象層 `MarketDataSource` と dry-run 用の `StubMarketDataSource` を提供する。
live 実装時は `GmoMarketDataSource` を追加し、MarketWatcher のコンストラクタで
差し替える（設定 or 環境変数で選択）。
"""
from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# データ型
# ----------------------------------------------------------------------
@dataclass
class Ticker:
    symbol: str
    last: float
    bid: float
    ask: float
    volume: float
    ts: float


@dataclass
class Candle:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MarketSnapshot:
    ts: float = 0.0
    tickers: dict[str, Ticker] = field(default_factory=dict)
    ohlcv: dict[str, list[Candle]] = field(default_factory=dict)

    def symbols(self) -> list[str]:
        return list(self.tickers.keys())


# ----------------------------------------------------------------------
# 抽象層
# ----------------------------------------------------------------------
class MarketDataSource(ABC):
    @abstractmethod
    def fetch_tickers(self, symbols: list[str]) -> dict[str, Ticker]: ...

    @abstractmethod
    def fetch_ohlcv(self, symbols: list[str], n: int = 30) -> dict[str, list[Candle]]: ...


# ----------------------------------------------------------------------
# dry-run 用スタブ
# ----------------------------------------------------------------------
class StubMarketDataSource(MarketDataSource):
    """合成データソース。外部 API に依存しない。

    - シンボルごとの基準価格を持ち、小さな乱数ウォークで動かす
    - 再現性のため、プロセス内で同じ seed を使う
    """

    _BASE_PRICES: dict[str, float] = {
        "BTC_JPY": 10_000_000.0,
        "ETH_JPY": 500_000.0,
        "SOL_JPY": 20_000.0,
        "XRP_JPY": 100.0,
        "DOGE_JPY": 20.0,
    }

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed if seed is not None else int(time.time()))
        # サイクル間で価格が連続するように直近価格を保持
        self._last_prices: dict[str, float] = {}

    def _base(self, symbol: str) -> float:
        return self._BASE_PRICES.get(symbol, 1000.0)

    def _start_price(self, symbol: str) -> float:
        return self._last_prices.get(symbol) or self._base(symbol)

    def fetch_ohlcv(self, symbols: list[str], n: int = 30) -> dict[str, list[Candle]]:
        out: dict[str, list[Candle]] = {}
        now = time.time()
        for sym in symbols:
            start = self._start_price(sym)
            candles: list[Candle] = []
            price = start
            for i in range(n):
                # 若干の上方バイアスで、たまに買い候補が出るようにする
                step = self._rng.uniform(-0.010, 0.012)
                new = price * (1.0 + step)
                jitter_h = abs(self._rng.uniform(0.0, 0.003))
                jitter_l = abs(self._rng.uniform(0.0, 0.003))
                candles.append(Candle(
                    ts=now - (n - i) * 300.0,
                    open=price,
                    high=max(price, new) * (1.0 + jitter_h),
                    low=min(price, new) * (1.0 - jitter_l),
                    close=new,
                    volume=self._rng.uniform(1.0, 100.0),
                ))
                price = new
            out[sym] = candles
            self._last_prices[sym] = price  # 次サイクルへ連続させる
        return out

    def fetch_tickers(self, symbols: list[str]) -> dict[str, Ticker]:
        out: dict[str, Ticker] = {}
        now = time.time()
        for sym in symbols:
            # 直近 OHLCV の終値から小さな揺れだけ。price_gap で HALT しない範囲。
            prev = self._start_price(sym)
            last = prev * (1.0 + self._rng.uniform(-0.002, 0.002))
            spread = last * 0.0005
            out[sym] = Ticker(
                symbol=sym,
                last=last,
                bid=last - spread,
                ask=last + spread,
                volume=self._rng.uniform(1.0, 100.0),
                ts=now,
            )
            self._last_prices[sym] = last
        return out


# ----------------------------------------------------------------------
# MarketWatcher
# ----------------------------------------------------------------------
class MarketWatcher:
    def __init__(self, cfg: dict[str, Any], source: MarketDataSource | None = None) -> None:
        self.cfg = cfg
        self.symbols: list[str] = self._collect_symbols(cfg)
        self.ohlcv_n: int = self._required_ohlcv_n(cfg)
        # TODO(live): 環境変数/設定で source を切り替える。
        #   live: GmoMarketDataSource（GMOコイン Public API /v1/ticker, /v1/klines）。
        #   dry_run: StubMarketDataSource（このまま）。
        self.source: MarketDataSource = source or StubMarketDataSource()

    @staticmethod
    def _collect_symbols(cfg: dict[str, Any]) -> list[str]:
        s = cfg.get("symbols", {}) or {}
        return list(s.get("core", [])) + list(s.get("satellite", []))

    @staticmethod
    def _required_ohlcv_n(cfg: dict[str, Any]) -> int:
        """scorer の窓長から必要本数を逆算。余裕を見て +10 本取る。"""
        sc = cfg.get("scorer", {}) or {}
        trend = sc.get("trend") or {}
        needs = [
            int(trend.get("long_ma", 20)),
            int((sc.get("heat") or {}).get("window", 5)),
            int((sc.get("liquidity") or {}).get("window", 10)),
            int((sc.get("volatility") or {}).get("window", 20)),
        ]
        return max(max(needs) + 10, 30)

    def fetch(self) -> MarketSnapshot:
        """1 サイクル分の市場スナップショットを取得する。

        例外は上位（main.run_cycle）でキャッチして HALT 判定する。
        """
        logger.info("market_watcher.fetch symbols=%s n=%d", self.symbols, self.ohlcv_n)
        # ohlcv → tickers の順。スタブは最終 close を保持し、ticker はそれを基準にする。
        # live 実装でも ohlcv と ticker の時間整合が取りやすい順序。
        ohlcv = self.source.fetch_ohlcv(self.symbols, n=self.ohlcv_n)
        tickers = self.source.fetch_tickers(self.symbols)
        return MarketSnapshot(ts=time.time(), tickers=tickers, ohlcv=ohlcv)
