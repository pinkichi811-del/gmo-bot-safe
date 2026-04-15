"""外部レジーム情報フィルター。

「今ロングしてよい地合いか」だけを判定する補助層。direction 予測はしない。

フィルター一覧:
  - us_market_hours_filter: 米株の時間帯で許可/拒否
  - index_trend_filter: SPX or NDX が MA_short > MA_long の時のみ許可
  - event_window_filter: 主要経済指標の前後 N 分は拒否
  - vix_filter: VIX が閾値以下の時のみ許可

すべて「通過時 True / 阻止時 False」を返す純関数。
"""
from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# 米株時間帯（UTC 基準）
# ---------------------------------------------------------------------------
# 通常市場: 14:30-21:00 UTC（9:30-16:00 ET, 標準時）
# プレマーケット: 09:00-14:30 UTC
# 引け後: 21:00-00:00 UTC
# アジア時間: 00:00-09:00 UTC
# 実際は DST で 1H ズレるが、検証目的なら標準時で固定する。
def us_session(ts_utc: float) -> str:
    dt = datetime.fromtimestamp(ts_utc, tz=timezone.utc)
    hm = dt.hour + dt.minute / 60.0
    if 14.5 <= hm < 21.0:
        return "us_regular"
    if 9.0 <= hm < 14.5:
        return "us_premarket"
    if 21.0 <= hm < 24.0:
        return "us_afterhours"
    return "asia"


def filter_us_regular_only(ts_utc: float) -> bool:
    return us_session(ts_utc) == "us_regular"


def filter_us_regular_or_pre(ts_utc: float) -> bool:
    return us_session(ts_utc) in ("us_regular", "us_premarket")


def filter_not_asia(ts_utc: float) -> bool:
    return us_session(ts_utc) != "asia"


# ---------------------------------------------------------------------------
# 指数（SPX / NDX / VIX）日足 CSV ロード
# ---------------------------------------------------------------------------
@dataclass
class DailyBar:
    ts: float
    date: str
    open: float
    high: float
    low: float
    close: float


def load_daily_csv(path: Path) -> list[DailyBar]:
    rows: list[DailyBar] = []
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                rows.append(DailyBar(
                    ts=float(row["ts"]),
                    date=row["date"],
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                ))
            except (ValueError, KeyError):
                continue
    rows.sort(key=lambda b: b.ts)
    return rows


# ---------------------------------------------------------------------------
# 指数トレンドフィルター
# ---------------------------------------------------------------------------
def make_index_trend_filter(
    bars: list[DailyBar], ma_short: int = 5, ma_long: int = 20,
) -> Callable[[float], bool]:
    """MA_short > MA_long の日だけ True を返す関数。
    bar の close 時刻は US market close (~21:00 UTC) として扱い、
    BTC bar の ts より前に confirmed な直近 index bar を参照する。
    """
    closes = [b.close for b in bars]
    ts_list = [b.ts for b in bars]
    n = len(bars)

    # precompute MAs
    sma_s = [0.0] * n
    sma_l = [0.0] * n
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= ma_short:
            s -= closes[i - ma_short]
        if i >= ma_short - 1:
            sma_s[i] = s / ma_short
    s = 0.0
    for i in range(n):
        s += closes[i]
        if i >= ma_long:
            s -= closes[i - ma_long]
        if i >= ma_long - 1:
            sma_l[i] = s / ma_long

    def f(ts_utc: float) -> bool:
        # 一つ前に confirmed な bar を探す
        idx = bisect.bisect_right(ts_list, ts_utc) - 1
        if idx < ma_long:
            return True  # データ不足時は通過
        if sma_l[idx] <= 0:
            return True
        return sma_s[idx] > sma_l[idx]

    return f


def make_index_momentum_filter(
    bars: list[DailyBar], lookback: int = 3,
) -> Callable[[float], bool]:
    """直近 lookback 日の終値モメンタムが正（上昇）の時だけ True。"""
    closes = [b.close for b in bars]
    ts_list = [b.ts for b in bars]

    def f(ts_utc: float) -> bool:
        idx = bisect.bisect_right(ts_list, ts_utc) - 1
        if idx < lookback:
            return True
        return closes[idx] > closes[idx - lookback]

    return f


# ---------------------------------------------------------------------------
# VIX フィルター
# ---------------------------------------------------------------------------
def make_vix_filter(
    bars: list[DailyBar], max_vix: float = 25.0,
) -> Callable[[float], bool]:
    """VIX 終値が閾値以下の時だけ True（リスクオフ時停止）。"""
    closes = [b.close for b in bars]
    ts_list = [b.ts for b in bars]

    def f(ts_utc: float) -> bool:
        idx = bisect.bisect_right(ts_list, ts_utc) - 1
        if idx < 0:
            return True
        return closes[idx] <= max_vix

    return f


# ---------------------------------------------------------------------------
# 経済指標イベントカレンダー
# ---------------------------------------------------------------------------
# 高インパクトイベントの発表時刻（UTC）
# FOMC は 18:00 UTC 頃、NFP/CPI/PPI は 13:30 UTC が多い
FOMC_DATES = [
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18",
]


def _first_weekday_of_month(year: int, month: int, weekday: int) -> date:
    """weekday: Monday=0 ... Sunday=6"""
    d = date(year, month, 1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


def _second_weekday_of_month(year: int, month: int, weekday: int) -> date:
    return _first_weekday_of_month(year, month, weekday) + timedelta(days=7)


def generate_events_calendar(start_year: int = 2024, end_year: int = 2026) -> list[tuple[float, str]]:
    """(timestamp_utc, event_name) のリストを返す。"""
    events: list[tuple[float, str]] = []

    # FOMC (18:00 UTC = 2pm ET)
    for ds in FOMC_DATES:
        y, m, d = map(int, ds.split("-"))
        ts = datetime(y, m, d, 18, 0, tzinfo=timezone.utc).timestamp()
        events.append((ts, "FOMC"))

    # NFP: 第一金曜 13:30 UTC (標準時 8:30 ET)
    # CPI: 第二火曜 13:30 UTC (approximate)
    # PPI: 第二水曜 13:30 UTC (approximate)
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            try:
                nfp = _first_weekday_of_month(year, month, 4)  # Friday
                events.append((datetime(
                    nfp.year, nfp.month, nfp.day, 13, 30, tzinfo=timezone.utc,
                ).timestamp(), "NFP"))
                cpi = _second_weekday_of_month(year, month, 1)  # Tuesday
                events.append((datetime(
                    cpi.year, cpi.month, cpi.day, 13, 30, tzinfo=timezone.utc,
                ).timestamp(), "CPI"))
                ppi = _second_weekday_of_month(year, month, 2)  # Wednesday
                events.append((datetime(
                    ppi.year, ppi.month, ppi.day, 13, 30, tzinfo=timezone.utc,
                ).timestamp(), "PPI"))
            except ValueError:
                continue

    events.sort(key=lambda e: e[0])
    return events


def make_event_avoidance_filter(
    events: list[tuple[float, str]],
    minutes_before: int = 30, minutes_after: int = 30,
) -> Callable[[float], bool]:
    """event 前後 minutes_before/after 分は False を返す。"""
    ev_ts = [e[0] for e in events]

    def f(ts_utc: float) -> bool:
        # bisect で近傍 event 探索
        idx = bisect.bisect_left(ev_ts, ts_utc)
        # check both neighbors
        for k in (idx - 1, idx):
            if 0 <= k < len(ev_ts):
                diff = ev_ts[k] - ts_utc
                if -minutes_after * 60 <= diff <= minutes_before * 60:
                    return False
        return True

    return f
