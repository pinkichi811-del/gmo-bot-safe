#!/usr/bin/env python3
"""観察ログ (data/score_log/*.jsonl) と注文ログ (data/dry_run_orders.csv) を集計する。

使用例:
  python scripts/aggregate.py                       # 今日（UTC）
  python scripts/aggregate.py --date 2026-04-13     # 特定日
  python scripts/aggregate.py --days 7              # 直近7日
  python scripts/aggregate.py --days 14 --show-trades

stdlib のみ。追加依存なし。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

# Phase 2 観察用: 再起動跨ぎでスタブ価格と state.json が不整合になり巨大損失が
# 記録される件 (memory: project_dryrun_phantom_loss) を集計から除外するための閾値。
# stop_loss_pct 設定 (-2.0 ~ -4.0) より十分深く、実例の偽損失 (-48%) より浅い。
DEFAULT_FAKE_LOSS_PCT = -30.0


def iter_records(paths: Iterable[Path]) -> Iterable[dict]:
    for p in paths:
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


# ---------------------------------------------------------------------------
# 注文ログ (dry_run_orders.csv) の解析
# ---------------------------------------------------------------------------

# 符号付きパーセント抽出 (例: "stop_loss -2.50%", "take_profit +8.31%")
_PCT_RE = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")


def parse_realized_pct(reason: str) -> float | None:
    """sell の reason から実現損益 % を抽出。buy / 不明形式は None。"""
    if not reason:
        return None
    if reason.startswith(("stop_loss", "take_profit", "trail")):
        m = _PCT_RE.search(reason)
        return float(m.group(1)) if m else None
    if reason.startswith("max_hold"):
        # "max_hold 300bars pnl=+4.88%" の pnl= 部分を取り出す
        m = re.search(r"pnl=([+-]?\d+(?:\.\d+)?)", reason)
        return float(m.group(1)) if m else None
    return None


def is_fake_loss(side: str, reason: str, threshold_pct: float) -> bool:
    """state.json と stub 価格の不整合で記録される偽損失を判定。"""
    if side != "sell":
        return False
    pct = parse_realized_pct(reason)
    return pct is not None and pct < threshold_pct


def iter_orders(
    csv_path: Path, start: date, end: date,
) -> Iterable[dict]:
    """orders.csv を iso_ts で start..end に絞ってイテレート。"""
    if not csv_path.exists():
        return
    with csv_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iso_ts = row.get("iso_ts", "")
            if not iso_ts:
                continue
            try:
                d = datetime.strptime(iso_ts[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < start or d > end:
                continue
            yield row


def compute_pnl_stats(
    orders: Iterable[dict], initial_cash: float, fake_loss_threshold: float,
) -> dict:
    """sell 行の reason から PnL/PF/DD を集計する。buy はサイズ参照のみ。"""
    sells: list[dict] = []
    fake_excluded = 0
    for row in orders:
        side = row.get("side", "")
        reason = row.get("reason", "")
        if side != "sell":
            continue
        if is_fake_loss(side, reason, fake_loss_threshold):
            fake_excluded += 1
            continue
        pct = parse_realized_pct(reason)
        if pct is None:
            continue
        try:
            size_jpy = float(row.get("size_jpy", 0) or 0)
        except ValueError:
            continue
        pnl_jpy = size_jpy * pct / 100.0
        sells.append({
            "iso_ts": row.get("iso_ts", ""),
            "symbol": row.get("symbol", "?"),
            "pct": pct,
            "size_jpy": size_jpy,
            "pnl_jpy": pnl_jpy,
        })

    sells.sort(key=lambda s: s["iso_ts"])

    wins = [s for s in sells if s["pnl_jpy"] > 0]
    losses = [s for s in sells if s["pnl_jpy"] < 0]
    total_pnl = sum(s["pnl_jpy"] for s in sells)
    gross_win = sum(s["pnl_jpy"] for s in wins)
    gross_loss = abs(sum(s["pnl_jpy"] for s in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    # 累積 PnL とドローダウン (initial_cash 比)
    cum = 0.0
    peak = 0.0
    max_dd_jpy = 0.0
    for s in sells:
        cum += s["pnl_jpy"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd_jpy:
            max_dd_jpy = dd

    pnl_pct = (total_pnl / initial_cash * 100.0) if initial_cash > 0 else 0.0
    dd_pct = (max_dd_jpy / initial_cash * 100.0) if initial_cash > 0 else 0.0

    return {
        "trades": len(sells),
        "wins": len(wins),
        "losses": len(losses),
        "pnl_jpy": total_pnl,
        "pnl_pct": pnl_pct,
        "profit_factor": pf,
        "max_dd_jpy": max_dd_jpy,
        "max_dd_pct": dd_pct,
        "fake_excluded": fake_excluded,
        "trades_detail": sells,
    }


def compute_regime_stats(records: Iterable[dict]) -> dict:
    """record['regime'] が記録されているサイクルから block 率を計算。"""
    total = 0
    blocked = 0
    for rec in records:
        regime = rec.get("regime")
        if not isinstance(regime, dict):
            continue
        total += 1
        if regime.get("allow_buy") is False:
            blocked += 1
    rate = (blocked / total * 100.0) if total > 0 else 0.0
    return {"total": total, "blocked": blocked, "block_rate_pct": rate}


def compute_cash_ratio_stats(records: Iterable[dict], min_threshold: float = 0.20) -> dict:
    """record['portfolio']['cash_ratio'] の min/mean/below 件数を集計。"""
    values: list[float] = []
    below = 0
    for rec in records:
        pf = rec.get("portfolio") or {}
        if "cash_ratio" not in pf:
            continue
        try:
            v = float(pf["cash_ratio"])
        except (TypeError, ValueError):
            continue
        values.append(v)
        if v < min_threshold:
            below += 1
    if not values:
        return {"n": 0, "min": None, "mean": None, "below_count": 0,
                "min_threshold": min_threshold}
    return {
        "n": len(values),
        "min": min(values),
        "mean": sum(values) / len(values),
        "below_count": below,
        "min_threshold": min_threshold,
    }


def _load_initial_cash_from_config(state_dir: Path) -> float:
    """app.yaml が読めれば portfolio.initial_cash_jpy を返す。失敗時は 1_000_000。"""
    cfg_path = Path(os.environ.get("CONFIG_PATH", "")) if os.environ.get("CONFIG_PATH") else None
    if not cfg_path or not cfg_path.exists():
        cfg_path = state_dir.parent / "config" / "app.yaml"
    if not cfg_path.exists():
        return 1_000_000.0
    try:
        import yaml  # type: ignore
    except ImportError:
        return 1_000_000.0
    try:
        with cfg_path.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return float((cfg.get("portfolio") or {}).get("initial_cash_jpy", 1_000_000))
    except Exception:
        return 1_000_000.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", help="YYYY-MM-DD (UTC). 省略時は今日")
    ap.add_argument("--days", type=int, default=1, help="終了日から何日分さかのぼるか")
    ap.add_argument("--state-dir", default=os.environ.get("STATE_DIR", "./data"))
    ap.add_argument("--top", type=int, default=20, help="verdict 分布で表示する上位数")
    ap.add_argument("--orders-csv", default=None,
                    help="dry_run_orders.csv のパス (default: <state-dir>/dry_run_orders.csv)")
    ap.add_argument("--fake-loss-pct", type=float, default=DEFAULT_FAKE_LOSS_PCT,
                    help=f"これより深い stop_loss/max_hold は偽損失として除外 "
                         f"(default: {DEFAULT_FAKE_LOSS_PCT})")
    ap.add_argument("--initial-cash", type=float, default=None,
                    help="PnL%/DD% の母数。省略時は config/app.yaml の portfolio.initial_cash_jpy")
    ap.add_argument("--show-trades", action="store_true",
                    help="各 sell の損益を時系列で出力する")
    args = ap.parse_args()

    state_dir = Path(args.state_dir)
    base = state_dir / "score_log"
    if not base.exists():
        print(f"[warn] {base} が存在しません。dry-run を一度回してください。", file=sys.stderr)
        return 1

    end: date
    if args.date:
        end = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=max(args.days, 1) - 1)

    paths: list[Path] = []
    d = start
    while d <= end:
        paths.append(base / f"{d.isoformat()}.jsonl")
        d += timedelta(days=1)

    total = 0
    halted = 0
    stop_cycles = 0
    errors: Counter = Counter()
    verdicts: Counter = Counter()
    buy_cand: Counter = Counter()
    strong_cand: Counter = Counter()
    decisions: dict[str, dict[str, int]] = defaultdict(lambda: {"buy": 0, "sell": 0})
    sums: dict[str, dict[str, float]] = defaultdict(lambda: {
        "total": 0.0, "trend": 0.0, "liquidity": 0.0, "heat": 0.0,
        "volatility": 0.0, "dup_penalty": 0.0, "cash_bonus": 0.0, "n": 0,
    })

    records = list(iter_records(paths))
    for rec in records:
        total += 1
        if rec.get("halted"):
            halted += 1
        if rec.get("stop_file"):
            stop_cycles += 1
        for e in rec.get("errors", []) or []:
            errors[str(e)[:80]] += 1
        for ev in rec.get("evaluations", []) or []:
            verdicts[ev.get("verdict", "?")] += 1
            sym = ev.get("symbol", "?")
            if ev.get("buy_candidate"):
                buy_cand[sym] += 1
            if ev.get("strong_buy"):
                strong_cand[sym] += 1
            agg = sums[sym]
            agg["n"] = int(agg["n"]) + 1
            for k in ("total", "trend", "liquidity", "heat",
                      "volatility", "dup_penalty", "cash_bonus"):
                agg[k] += float(ev.get(k, 0.0) or 0.0)
        for dd in rec.get("decisions", []) or []:
            decisions[dd.get("symbol", "?")][dd.get("side", "?")] += 1

    period = (
        f"{start.isoformat()}" if start == end
        else f"{start.isoformat()} … {end.isoformat()}"
    )
    print(f"=== aggregate: {period} (UTC) ===")
    print(f"cycles={total}  halted={halted}  stop_file={stop_cycles}")

    if errors:
        print("\n-- errors --")
        for k, v in errors.most_common(10):
            print(f"  {v:4d}  {k}")

    print("\n-- verdict distribution --")
    for k, v in verdicts.most_common(args.top):
        print(f"  {v:4d}  {k}")

    if sums:
        print("\n-- average scores by symbol --")
        hdr = f"  {'symbol':<10} {'total':>6} {'trend':>6} {'liq':>6} {'heat':>6} {'vol':>6} {'dup':>6} {'cash':>6}   n"
        print(hdr)
        for sym, a in sorted(sums.items()):
            n = int(a["n"]) or 1
            print(f"  {sym:<10} "
                  f"{a['total']/n:6.1f} {a['trend']/n:6.1f} "
                  f"{a['liquidity']/n:6.1f} {a['heat']/n:6.1f} "
                  f"{a['volatility']/n:6.1f} {a['dup_penalty']/n:6.1f} "
                  f"{a['cash_bonus']/n:6.1f}   {int(a['n'])}")

    if buy_cand:
        print("\n-- buy_candidate counts --")
        for sym, c in buy_cand.most_common():
            print(f"  {c:4d}  {sym} (strong={strong_cand.get(sym, 0)})")

    if decisions:
        print("\n-- decisions by symbol --")
        for sym, d in sorted(decisions.items()):
            print(f"  {sym:<10} buy={d['buy']} sell={d['sell']}")

    # ------------------------------------------------------------------
    # PnL / PF / max DD (dry_run_orders.csv より)
    # ------------------------------------------------------------------
    orders_csv = Path(args.orders_csv) if args.orders_csv \
        else state_dir / "dry_run_orders.csv"
    initial_cash = (
        args.initial_cash if args.initial_cash is not None
        else _load_initial_cash_from_config(state_dir)
    )

    if orders_csv.exists():
        orders = list(iter_orders(orders_csv, start, end))
        pnl = compute_pnl_stats(orders, initial_cash, args.fake_loss_pct)
        print("\n-- pnl summary "
              f"(initial_cash={initial_cash:,.0f} JPY, "
              f"fake_loss< {args.fake_loss_pct:+.1f}% excluded={pnl['fake_excluded']}) --")
        if pnl["trades"] == 0:
            print("  (no realized trades in period)")
        else:
            pf_str = f"{pnl['profit_factor']:.2f}" \
                if pnl["profit_factor"] != float("inf") else "inf"
            print(f"  trades={pnl['trades']} wins={pnl['wins']} losses={pnl['losses']}")
            print(f"  pnl={pnl['pnl_jpy']:+,.0f} JPY ({pnl['pnl_pct']:+.2f}%)  "
                  f"PF={pf_str}  "
                  f"max_DD={pnl['max_dd_jpy']:,.0f} JPY ({pnl['max_dd_pct']:.2f}%)")
            if args.show_trades:
                print("\n-- trades detail --")
                for t in pnl["trades_detail"]:
                    print(f"  {t['iso_ts']}  {t['symbol']:<10} "
                          f"{t['pct']:+6.2f}%  {t['pnl_jpy']:+,.0f} JPY  "
                          f"size={t['size_jpy']:,.0f}")
    else:
        print(f"\n-- pnl summary -- (orders csv not found: {orders_csv})")

    # ------------------------------------------------------------------
    # regime gate block 率
    # ------------------------------------------------------------------
    regime = compute_regime_stats(records)
    if regime["total"] > 0:
        print(f"\n-- regime gate -- blocks={regime['blocked']} / "
              f"total={regime['total']} cycles ({regime['block_rate_pct']:.1f}%)")
    else:
        print("\n-- regime gate -- (no regime records; filter disabled)")

    # ------------------------------------------------------------------
    # cash_ratio (新規買いの 20% ガードに対する余裕)
    # ------------------------------------------------------------------
    cr = compute_cash_ratio_stats(records)
    if cr["n"] > 0:
        print(f"\n-- cash_ratio -- min={cr['min']:.3f} mean={cr['mean']:.3f}  "
              f"below_{cr['min_threshold']:.2f}={cr['below_count']} cycles  "
              f"(n={cr['n']})")
    else:
        print("\n-- cash_ratio -- (no portfolio.cash_ratio records)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
