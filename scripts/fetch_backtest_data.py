#!/usr/bin/env python3
"""GMOコイン Public API から 5分足 OHLCV を取得して JSON で保存する。

エンドポイント: https://api.coin.z.com/public/v1/klines?symbol=<SYM>&interval=5min&date=YYYYMMDD
  - symbol は現物識別子（BTC, ETH, SOL, XRP, DOGE）。bot 内部名 BTC_JPY 等から _JPY を外す。
  - date は JST (Asia/Tokyo)。1 日= 最大 288 本（5分 × 288 = 24h）。
  - 取引量 0 の 5分 bar は返ってこないことがある（そのまま欠損として扱う）。

使用例:
  python scripts/fetch_backtest_data.py --start 20260228 --end 20260401
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

API = "https://api.coin.z.com/public/v1/klines"

# bot 内部名 → API 用 symbol
SYMBOL_MAP = {
    "BTC_JPY": "BTC",
    "ETH_JPY": "ETH",
    "SOL_JPY": "SOL",
    "XRP_JPY": "XRP",
    "DOGE_JPY": "DOGE",
}


def fetch_day(api_sym: str, jst_date: str, timeout: float = 15.0) -> dict:
    url = f"{API}?symbol={api_sym}&interval=5min&date={jst_date}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "gmo-bot-safe-backtest/1.0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def daterange(start: str, end: str):
    s = datetime.strptime(start, "%Y%m%d").date()
    e = datetime.strptime(end, "%Y%m%d").date()
    d = s
    while d <= e:
        yield d.strftime("%Y%m%d")
        d += timedelta(days=1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", required=True, help="JST YYYYMMDD (inclusive)")
    ap.add_argument("--end", required=True, help="JST YYYYMMDD (inclusive)")
    ap.add_argument("--out", default="./data/backtest/raw")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--symbols", nargs="+", default=list(SYMBOL_MAP.keys()))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    total, skipped, fetched, errors = 0, 0, 0, 0
    for bot_sym in args.symbols:
        api_sym = SYMBOL_MAP.get(bot_sym, bot_sym)
        for jst in daterange(args.start, args.end):
            total += 1
            out_path = out_dir / f"{bot_sym}_{jst}.json"
            if out_path.exists():
                skipped += 1
                continue
            try:
                resp = fetch_day(api_sym, jst)
                if resp.get("status") != 0:
                    print(f"[warn] {bot_sym} {jst} non-zero status: {resp}",
                          file=sys.stderr)
                    errors += 1
                    continue
                payload = {
                    "bot_symbol": bot_sym,
                    "api_symbol": api_sym,
                    "jst_date": jst,
                    "fetched_at": datetime.utcnow().isoformat() + "Z",
                    "count": len(resp.get("data", [])),
                    "candles": resp.get("data", []),
                }
                out_path.write_text(
                    json.dumps(payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                fetched += 1
                print(f"[ok ] {bot_sym} {jst}  n={payload['count']}")
            except urllib.error.URLError as e:
                print(f"[err] {bot_sym} {jst} {e}", file=sys.stderr)
                errors += 1
            time.sleep(args.sleep)

    print(f"\ndone. total={total} fetched={fetched} skipped={skipped} errors={errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
