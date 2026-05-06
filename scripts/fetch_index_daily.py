#!/usr/bin/env python3
"""Yahoo Finance v8 chart API から NDX / SPX / VIX 日足を取得し、
data/market/*_d.csv にマージする。

balanced_ndx champion の regime filter は data/market/NDX_d.csv の MA(5)/MA(10) を
見て買い可否を判定するため、CSV が古いと filter の判定も古い地合いで止まる。
このスクリプトは:
  - Yahoo Finance の chart endpoint から最新の日足 JSON を取得し
  - 既存 data/market/<INDEX>_d.csv の format (ts,date,open,high,low,close) に変換し
  - 日付キーで重複排除しつつマージ（新しいデータで上書き）

Yahoo Finance の ts は既存 CSV と同じく US market open (UTC 14:30 / 13:30) ベースで
返ってくるため、ts オフセット計算は不要。

stdlib のみ使用。

使用例:
  python scripts/fetch_index_daily.py                          # NDX (default)
  python scripts/fetch_index_daily.py --indices NDX,SPX,VIX
  python scripts/fetch_index_daily.py --indices NDX --start 2026-04-01
  python scripts/fetch_index_daily.py --dry-run                # 保存せず差分のみ表示
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo Finance のシンボル
YAHOO_SYMBOLS = {
    "NDX": "^NDX",
    "SPX": "^GSPC",
    "VIX": "^VIX",
}


def fetch_yahoo_chart(symbol: str, period1: int = 0, period2: int | None = None,
                     timeout: float = 30.0) -> dict:
    """Yahoo Finance chart API から daily OHLC を取得し JSON を返す。"""
    if period2 is None:
        period2 = int(datetime.now(tz=timezone.utc).timestamp()) + 86400
    qs = urllib.parse.urlencode({
        "period1": period1,
        "period2": period2,
        "interval": "1d",
    })
    enc_sym = urllib.parse.quote(symbol, safe="")
    url = f"{YAHOO_CHART.format(symbol=enc_sym)}?{qs}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (gmo-bot-safe-fetcher/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_yahoo_chart(payload: dict, start: date | None = None) -> list[dict]:
    """Yahoo Finance chart JSON を内部形式 dict に変換。"""
    bars: list[dict] = []
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"yahoo error: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        return bars
    res0 = results[0]
    timestamps = res0.get("timestamp") or []
    quote = (res0.get("indicators") or {}).get("quote") or [{}]
    q0 = quote[0] if quote else {}
    opens = q0.get("open") or []
    highs = q0.get("high") or []
    lows = q0.get("low") or []
    closes = q0.get("close") or []
    n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
    for i in range(n):
        ts = timestamps[i]
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if ts is None or o is None or h is None or l is None or c is None:
            continue
        d_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if start and date.fromisoformat(d_str) < start:
            continue
        bars.append({
            "ts": int(ts),
            "date": d_str,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
        })
    return bars


def merge_with_existing(new_bars: list[dict], path: Path) -> list[dict]:
    """既存 CSV を読んで日付キーで重複排除、新しい方で上書き、ts 昇順で返す。"""
    existing: dict[str, dict] = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                if not row.get("date"):
                    continue
                try:
                    existing[row["date"]] = {
                        "ts": int(float(row["ts"])),
                        "date": row["date"],
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                    }
                except (KeyError, ValueError):
                    continue
    for b in new_bars:
        existing[b["date"]] = b
    return sorted(existing.values(), key=lambda b: b["ts"])


def write_csv(bars: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "date", "open", "high", "low", "close"])
        for b in bars:
            w.writerow([b["ts"], b["date"], b["open"], b["high"], b["low"], b["close"]])


def _existing_last_date(path: Path) -> str:
    if not path.exists():
        return ""
    with path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        rows = list(r)
    return rows[-1]["date"] if rows else ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--indices", default="NDX",
                   help="取得対象 (NDX,SPX,VIX をカンマ区切り)。default: NDX")
    p.add_argument("--out-dir", default="./data/market", help="出力ディレクトリ")
    p.add_argument("--start", default=None,
                   help="この日以降のみ反映 (YYYY-MM-DD)。既存 CSV は保持される")
    p.add_argument("--dry-run", action="store_true",
                   help="保存せず差分だけ表示")
    args = p.parse_args(argv)

    indices = [s.strip().upper() for s in args.indices.split(",") if s.strip()]
    out_dir = Path(args.out_dir)
    start = date.fromisoformat(args.start) if args.start else None

    rc = 0
    for ix in indices:
        if ix not in YAHOO_SYMBOLS:
            print(f"[fetch_index] unknown index: {ix}", file=sys.stderr)
            rc = 1
            continue
        path = out_dir / f"{ix}_d.csv"
        try:
            payload = fetch_yahoo_chart(YAHOO_SYMBOLS[ix])
            new_bars = parse_yahoo_chart(payload, start=start)
            if not new_bars:
                print(f"[fetch_index] {ix}: empty result from Yahoo", file=sys.stderr)
                rc = 1
                continue
            existing_last = _existing_last_date(path)
            merged = merge_with_existing(new_bars, path)
            if args.dry_run:
                print(f"[fetch_index] {ix}: existing_last={existing_last or '(none)'} "
                      f"new_count={len(new_bars)} merged_count={len(merged)} "
                      f"merged_last={merged[-1]['date']} (dry-run, not written)")
            else:
                write_csv(merged, path)
                print(f"[fetch_index] {ix}: wrote {len(merged)} bars to {path} "
                      f"(was {existing_last or '(none)'} -> now {merged[-1]['date']})")
        except Exception as e:
            print(f"[fetch_index] {ix} failed: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
