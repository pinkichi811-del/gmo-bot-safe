#!/usr/bin/env python3
"""GMOコイン bot の dry-run ロジックそのままで、2026-03 の月間バックテストを走らせる。

現行コード忠実版（fee=0 / slippage=0）と、fee を後付けで差し引いた版の 2 系統を出力する。
判定ロジック・順序は live 用の `src/` 配下をそのまま呼び、変更しない:
  - Scorer.score()
  - apply_cash_bonus()
  - RiskGuard.evaluate_sells()
  - RiskGuard.evaluate_buy_candidates()
  - RiskGuard.apply_portfolio_constraints()

状態（cooldown）だけは時計を time.time() から sim_time に差し替えるため
`BacktestStateStore` を使う。
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from market_watcher import Candle, MarketSnapshot, Ticker  # noqa: E402
from risk_guard import Decision, RiskGuard  # noqa: E402
from scorer import Scorer, apply_cash_bonus  # noqa: E402
from state_store import Position, StateStore  # noqa: E402


# ---------------------------------------------------------------------------
# state: sim_time で cooldown を回す
# ---------------------------------------------------------------------------
class BacktestStateStore(StateStore):
    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.sim_time: float = 0.0

    def set_cooldown(self, symbol: str, minutes: float) -> None:
        self._state.setdefault("cooldown_until", {})[symbol] = (
            self.sim_time + minutes * 60.0
        )

    def in_cooldown(self, symbol: str) -> bool:
        until = float((self._state.get("cooldown_until") or {}).get(symbol, 0.0))
        return self.sim_time < until

    def cooldown_remaining_sec(self, symbol: str) -> float:
        until = float((self._state.get("cooldown_until") or {}).get(symbol, 0.0))
        return max(0.0, until - self.sim_time)


# ---------------------------------------------------------------------------
# データロード & スナップショット構築
# ---------------------------------------------------------------------------
def load_candles(root: Path, symbols: list[str]) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = {}
    for sym in symbols:
        all_c: list[Candle] = []
        for fp in sorted(root.glob(f"{sym}_*.json")):
            payload = json.loads(fp.read_text(encoding="utf-8"))
            for c in payload["candles"]:
                all_c.append(Candle(
                    ts=int(c["openTime"]) / 1000.0,
                    open=float(c["open"]),
                    high=float(c["high"]),
                    low=float(c["low"]),
                    close=float(c["close"]),
                    volume=float(c["volume"]),
                ))
        all_c.sort(key=lambda x: x.ts)
        out[sym] = all_c
    return out


def build_snapshot(
    t: float,
    candles_by_sym: dict[str, list[Candle]],
    ts_lists: dict[str, list[float]],
    n_window: int = 30,
) -> MarketSnapshot:
    tickers: dict[str, Ticker] = {}
    ohlcv: dict[str, list[Candle]] = {}
    for sym, cs in candles_by_sym.items():
        idx = bisect.bisect_right(ts_lists[sym], t) - 1
        if idx < 0:
            continue
        window = cs[max(0, idx - n_window + 1): idx + 1]
        if not window:
            continue
        ohlcv[sym] = window
        latest = window[-1]
        # bid=ask=last で spread=0、live candle の close を last として代用
        tickers[sym] = Ticker(
            symbol=sym, last=latest.close,
            bid=latest.close, ask=latest.close,
            volume=latest.volume, ts=latest.ts,
        )
    return MarketSnapshot(ts=t, tickers=tickers, ohlcv=ohlcv)


def compute_equity_faithful(
    state: StateStore, snapshot: MarketSnapshot, initial_cash: float,
) -> tuple[float, float]:
    """現行コードの compute_equity と同じ簡易評価。fee は考慮しない。"""
    pos_value = 0.0
    used_cash = 0.0
    for sym, pos in state.positions().items():
        tk = snapshot.tickers.get(sym)
        cur = tk.last if tk else pos.entry_price
        if pos.entry_price > 0:
            units = pos.size_jpy / pos.entry_price
            pos_value += units * cur
        used_cash += pos.size_jpy
    cash = max(initial_cash - used_cash, 0.0)
    return cash, cash + pos_value


# ---------------------------------------------------------------------------
# バックテスト本体
# ---------------------------------------------------------------------------
def run_backtest(
    cfg: dict,
    candles_by_sym: dict[str, list[Candle]],
    start_iso: str,
    end_iso: str,
    tick_sec: int = 900,
) -> dict[str, Any]:
    initial_cash = float((cfg.get("portfolio") or {}).get("initial_cash_jpy", 1_000_000))
    cooldown_min = float((cfg.get("exits") or {}).get("cooldown_min", 180))

    ts_lists = {sym: [c.ts for c in cs] for sym, cs in candles_by_sym.items()}

    start = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()
    end = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp()
    ticks: list[float] = []
    t = start
    while t <= end:
        ticks.append(t)
        t += tick_sec

    with tempfile.TemporaryDirectory() as td:
        state = BacktestStateStore(path=str(Path(td) / "state.json"))
        scorer = Scorer(cfg)
        guard = RiskGuard(cfg, state)

        trades: list[dict[str, Any]] = []
        decisions_log: list[dict[str, Any]] = []
        verdicts_counter: Counter = Counter()
        halt_events: list[dict[str, Any]] = []
        equity_curve: list[dict[str, Any]] = []
        cycles_executed = 0
        skipped_no_data = 0

        for i, t in enumerate(ticks):
            state.sim_time = t

            if state.is_halted():
                continue

            snapshot = build_snapshot(t, candles_by_sym, ts_lists)
            if not snapshot.tickers:
                skipped_no_data += 1
                continue

            if not guard.health_check(snapshot):
                halt_events.append({"t": t, "reason": state.halt_reason()})
                continue

            scores = scorer.score(snapshot, held_symbols=state.positions().keys())

            cash, equity = compute_equity_faithful(state, snapshot, initial_cash)
            cash_ratio = (cash / equity) if equity > 0 else 0.0
            scores = apply_cash_bonus(scores, cash_ratio, cfg)

            sell_decisions: list[Decision] = guard.evaluate_sells(snapshot)
            passed, verdicts = guard.evaluate_buy_candidates(scores)
            buy_decisions, port_rej = guard.apply_portfolio_constraints(
                passed, snapshot, cash_jpy=cash, total_equity_jpy=equity,
            )

            # verdict 集計（cooldown 残分や dup 数値はバケット化）
            selected_syms = {d.symbol for d in buy_decisions}
            for v in verdicts:
                if v.symbol in selected_syms:
                    key = "selected"
                elif v.symbol in port_rej:
                    key = f"portfolio:{port_rej[v.symbol]}"
                elif v.passes:
                    key = "passed_but_not_selected"
                else:
                    r = v.reason
                    if r.startswith("cooldown("):
                        key = "cooldown"
                    elif r.startswith("dup_penalty("):
                        key = "dup_penalty"
                    else:
                        key = r  # below_threshold:... はそのまま（詳細分布のため）
                verdicts_counter[key] += 1

            # sell 実行（先）
            for d in sell_decisions:
                pos = state.positions().get(d.symbol)
                if pos is None or pos.entry_price <= 0:
                    continue
                units = pos.size_jpy / pos.entry_price
                proceeds = units * d.price_ref
                gross_pnl = proceeds - pos.size_jpy
                trades.append({
                    "symbol": d.symbol,
                    "entry_price": pos.entry_price,
                    "exit_price": d.price_ref,
                    "size_jpy": pos.size_jpy,
                    "entry_ts": pos.entry_ts,
                    "exit_ts": t,
                    "duration_min": (t - pos.entry_ts) / 60.0,
                    "close_reason": d.reason,
                    "gross_pnl_jpy": gross_pnl,
                    "gross_pnl_pct": gross_pnl / pos.size_jpy * 100.0,
                })
                state.remove_position(d.symbol)
                state.set_cooldown(d.symbol, minutes=cooldown_min)
                decisions_log.append({
                    "t": t, "symbol": d.symbol, "side": "sell",
                    "size_jpy": d.size_jpy, "price_ref": d.price_ref,
                    "reason": d.reason, "strong": d.strong,
                })

            # buy 実行（後）
            for d in buy_decisions:
                state.set_position(Position(
                    symbol=d.symbol, size_jpy=d.size_jpy,
                    entry_price=d.price_ref, entry_ts=t,
                ))
                decisions_log.append({
                    "t": t, "symbol": d.symbol, "side": "buy",
                    "size_jpy": d.size_jpy, "price_ref": d.price_ref,
                    "reason": d.reason, "strong": d.strong,
                })

            # equity 曲線を間引いて記録（1時間ごと=4tick）
            if i % 4 == 0 or i == len(ticks) - 1:
                _, eq_now = compute_equity_faithful(state, snapshot, initial_cash)
                realized = sum(tr["gross_pnl_jpy"] for tr in trades)
                equity_curve.append({
                    "t": t, "equity": eq_now,
                    "realized_pnl": realized,
                })
            cycles_executed += 1

        # 最終評価（未決済ポジションは成り行き評価のみ・自動クローズはしない）
        final_snap = build_snapshot(ticks[-1], candles_by_sym, ts_lists)
        _, final_equity = compute_equity_faithful(state, final_snap, initial_cash)
        open_positions: list[dict[str, Any]] = []
        for sym, pos in state.positions().items():
            tk = final_snap.tickers.get(sym)
            cur = tk.last if tk else pos.entry_price
            units = pos.size_jpy / pos.entry_price
            unrealized = units * cur - pos.size_jpy
            open_positions.append({
                "symbol": sym,
                "entry_price": pos.entry_price,
                "size_jpy": pos.size_jpy,
                "current_price": cur,
                "unrealized_pnl_jpy": unrealized,
                "unrealized_pnl_pct": unrealized / pos.size_jpy * 100.0,
                "entry_ts": pos.entry_ts,
            })

        return {
            "start_iso": start_iso,
            "end_iso": end_iso,
            "tick_sec": tick_sec,
            "total_ticks": len(ticks),
            "cycles_executed": cycles_executed,
            "skipped_no_data": skipped_no_data,
            "halt_events": halt_events,
            "initial_cash_jpy": initial_cash,
            "final_equity_jpy": final_equity,
            "trades": trades,
            "decisions_log": decisions_log,
            "verdicts": dict(verdicts_counter),
            "equity_curve": equity_curve,
            "final_open_positions": open_positions,
        }


# ---------------------------------------------------------------------------
# レポート生成
# ---------------------------------------------------------------------------
def _max_drawdown(curve: list[dict[str, Any]], key: str = "equity") -> float:
    peak = -float("inf")
    max_dd = 0.0
    for p in curve:
        v = p[key]
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def summarize(result: dict[str, Any], fee_rate: float = 0.0) -> dict[str, Any]:
    """fee_rate は buy/sell 片側ごとの料率（例: 0.0005 = 0.05%）。"""
    initial = result["initial_cash_jpy"]
    trades = result["trades"]

    # 各トレードに fee を適用
    fee_adjusted_trades = []
    total_gross = 0.0
    total_net = 0.0
    total_fee = 0.0
    wins_gross = wins_net = 0
    for tr in trades:
        size_jpy = tr["size_jpy"]
        gross = tr["gross_pnl_jpy"]
        buy_fee = size_jpy * fee_rate
        sell_fee = (size_jpy + gross) * fee_rate  # 約定金額ベースの sell fee
        fee = buy_fee + sell_fee
        net = gross - fee
        fee_adjusted_trades.append({
            **tr, "fee_jpy": fee, "net_pnl_jpy": net,
            "net_pnl_pct": net / size_jpy * 100.0,
        })
        total_gross += gross
        total_net += net
        total_fee += fee
        if gross > 0:
            wins_gross += 1
        if net > 0:
            wins_net += 1

    # open positions の未実現
    unrealized_gross = sum(p["unrealized_pnl_jpy"] for p in result["final_open_positions"])
    # open 側も fee を概算で乗せる（buy fee + 想定 sell fee）
    open_fees = 0.0
    for p in result["final_open_positions"]:
        size_jpy = p["size_jpy"]
        cur_notional = size_jpy + p["unrealized_pnl_jpy"]
        open_fees += size_jpy * fee_rate + cur_notional * fee_rate
    unrealized_net = unrealized_gross - open_fees

    # drawdown（gross 側を代表値とする）
    max_dd_gross = _max_drawdown(result["equity_curve"], "equity")

    # 銘柄別集計
    by_symbol: dict[str, dict[str, Any]] = {}
    for tr in fee_adjusted_trades:
        s = tr["symbol"]
        b = by_symbol.setdefault(s, {
            "trades": 0, "wins": 0, "gross_pnl": 0.0, "net_pnl": 0.0,
            "avg_hold_min": 0.0, "hold_sum": 0.0,
        })
        b["trades"] += 1
        b["gross_pnl"] += tr["gross_pnl_jpy"]
        b["net_pnl"] += tr["net_pnl_jpy"]
        b["hold_sum"] += tr["duration_min"]
        if tr["net_pnl_jpy"] > 0:
            b["wins"] += 1
    for s, b in by_symbol.items():
        b["avg_hold_min"] = b["hold_sum"] / b["trades"]
        b["win_rate"] = b["wins"] / b["trades"] * 100.0
        del b["hold_sum"]

    # 売り決済の分解（stop_loss / take_profit）
    sl = sum(1 for t in trades if t["close_reason"].startswith("stop_loss"))
    tp = sum(1 for t in trades if t["close_reason"].startswith("take_profit"))

    # 見送り理由の主要バケット
    verdicts = result["verdicts"]
    ver_buckets = {
        "selected": verdicts.get("selected", 0),
        "already_held": verdicts.get("already_held", 0),
        "cooldown": verdicts.get("cooldown", 0),
        "dup_penalty": verdicts.get("dup_penalty", 0),
        "stop_file": verdicts.get("stop_file", 0),
        "passed_but_not_selected": verdicts.get("passed_but_not_selected", 0),
        "portfolio:*": sum(v for k, v in verdicts.items() if k.startswith("portfolio:")),
        "below_threshold:*": sum(v for k, v in verdicts.items() if k.startswith("below_threshold:")),
    }

    return {
        "fee_rate": fee_rate,
        "initial_cash_jpy": initial,
        "final_equity_gross_jpy": initial + total_gross + unrealized_gross,
        "final_equity_net_jpy": initial + total_net + unrealized_net,
        "total_pnl_gross_jpy": total_gross + unrealized_gross,
        "total_pnl_net_jpy": total_net + unrealized_net,
        "total_pnl_gross_pct": (total_gross + unrealized_gross) / initial * 100.0,
        "total_pnl_net_pct": (total_net + unrealized_net) / initial * 100.0,
        "total_fees_jpy": total_fee + open_fees,
        "closed_trades": len(trades),
        "win_rate_gross_pct": wins_gross / len(trades) * 100.0 if trades else 0.0,
        "win_rate_net_pct": wins_net / len(trades) * 100.0 if trades else 0.0,
        "max_drawdown_pct_gross": max_dd_gross * 100.0,
        "stop_loss_count": sl,
        "take_profit_count": tp,
        "halt_count": len(result["halt_events"]),
        "cycles_executed": result["cycles_executed"],
        "skipped_no_data": result["skipped_no_data"],
        "total_ticks": result["total_ticks"],
        "by_symbol": by_symbol,
        "verdict_buckets": ver_buckets,
        "open_positions": result["final_open_positions"],
    }


def print_report(label: str, summary: dict[str, Any]) -> None:
    print(f"\n===== {label} =====")
    print(f"fee_rate={summary['fee_rate']*100:.3f}%  "
          f"ticks={summary['total_ticks']}  "
          f"cycles_executed={summary['cycles_executed']}  "
          f"skipped_no_data={summary['skipped_no_data']}")
    print(f"initial   : {summary['initial_cash_jpy']:>12,.0f} JPY")
    print(f"final gross: {summary['final_equity_gross_jpy']:>12,.0f} JPY  "
          f"(P/L {summary['total_pnl_gross_jpy']:+,.0f} / "
          f"{summary['total_pnl_gross_pct']:+.3f}%)")
    print(f"final net  : {summary['final_equity_net_jpy']:>12,.0f} JPY  "
          f"(P/L {summary['total_pnl_net_jpy']:+,.0f} / "
          f"{summary['total_pnl_net_pct']:+.3f}%)")
    print(f"total fees : {summary['total_fees_jpy']:>12,.0f} JPY")
    print(f"trades     : {summary['closed_trades']}  "
          f"(stop_loss={summary['stop_loss_count']}, "
          f"take_profit={summary['take_profit_count']})")
    print(f"win rate   : gross={summary['win_rate_gross_pct']:.1f}%  "
          f"net={summary['win_rate_net_pct']:.1f}%")
    print(f"max DD     : {summary['max_drawdown_pct_gross']:.3f}% (gross equity)")
    print(f"HALT events: {summary['halt_count']}")

    if summary["by_symbol"]:
        print("\n-- per symbol --")
        print(f"  {'symbol':<10} {'trades':>6} {'wins':>5} {'win%':>6} "
              f"{'gross_pnl':>10} {'net_pnl':>10} {'avg_hold(min)':>14}")
        for s, b in sorted(summary["by_symbol"].items()):
            print(f"  {s:<10} {b['trades']:>6} {b['wins']:>5} "
                  f"{b['win_rate']:>5.1f}% "
                  f"{b['gross_pnl']:>10,.0f} {b['net_pnl']:>10,.0f} "
                  f"{b['avg_hold_min']:>14.1f}")

    print("\n-- verdict buckets --")
    for k, v in summary["verdict_buckets"].items():
        print(f"  {k:<30} {v:>8}")

    if summary["open_positions"]:
        print("\n-- final open positions --")
        for p in summary["open_positions"]:
            print(f"  {p['symbol']:<10} size_jpy={p['size_jpy']:,.0f} "
                  f"entry={p['entry_price']:.2f} cur={p['current_price']:.2f} "
                  f"unrealized={p['unrealized_pnl_jpy']:+,.0f} "
                  f"({p['unrealized_pnl_pct']:+.2f}%)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-03-01T00:00:00Z",
                    help="backtest 開始 (UTC, ISO8601)")
    ap.add_argument("--end", default="2026-03-31T23:45:00Z",
                    help="backtest 終了 (UTC, ISO8601)")
    ap.add_argument("--data", default="./data/backtest/raw")
    ap.add_argument("--out", default="./data/backtest")
    ap.add_argument("--config", default="./config/app.yaml")
    ap.add_argument("--fee-rate", type=float, default=0.0005,
                    help="手数料加味版の片側料率 (0.0005 = 0.05%)")
    ap.add_argument("--trend", type=float, default=None,
                    help="buy_candidate.trend 閾値を上書き（例: 14）")
    ap.add_argument("--take-profit-pct", type=float, default=None,
                    help="exits.take_profit_pct を上書き（例: 4.0）")
    ap.add_argument("--stop-loss-pct", type=float, default=None,
                    help="exits.stop_loss_pct を上書き（例: -5.0）")
    ap.add_argument("--label", default="default",
                    help="出力ファイルと見出しに付けるラベル")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    # バックテスト中は STOP ファイル・HALT ファイル副作用を避ける
    cfg.setdefault("risk", {})["stop_file"] = "/__nonexistent__/STOP"

    # 閾値オーバーライド
    overrides: list[str] = []
    if args.trend is not None:
        thr = cfg.setdefault("scorer", {}).setdefault("thresholds", {})
        thr.setdefault("buy_candidate", {})["trend"] = args.trend
        overrides.append(f"buy_candidate.trend={args.trend}")
    if args.take_profit_pct is not None:
        cfg.setdefault("exits", {})["take_profit_pct"] = args.take_profit_pct
        overrides.append(f"take_profit_pct={args.take_profit_pct}")
    if args.stop_loss_pct is not None:
        cfg.setdefault("exits", {})["stop_loss_pct"] = args.stop_loss_pct
        overrides.append(f"stop_loss_pct={args.stop_loss_pct}")
    if overrides:
        print("[overrides] " + " ".join(overrides))

    symbols = (cfg.get("symbols") or {}).get("core", []) + \
              (cfg.get("symbols") or {}).get("satellite", [])

    data_root = Path(args.data)
    candles_by_sym = load_candles(data_root, symbols)
    for sym in symbols:
        print(f"[load] {sym}: {len(candles_by_sym.get(sym, []))} candles")

    print(f"\nrunning backtest {args.start} .. {args.end}")
    result = run_backtest(cfg, candles_by_sym, args.start, args.end)

    summary_zero = summarize(result, fee_rate=0.0)
    summary_fee = summarize(result, fee_rate=args.fee_rate)

    print_report("忠実版 (fee=0)", summary_zero)
    print_report(f"手数料加味版 (fee={args.fee_rate*100:.3f}%/片側)", summary_fee)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_{args.label}" if args.label != "default" else ""
    (out_dir / f"result_raw{suffix}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (out_dir / f"summary_fee_zero{suffix}.json").write_text(
        json.dumps(summary_zero, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    (out_dir / f"summary_fee_{int(args.fee_rate*10000)}bp{suffix}.json").write_text(
        json.dumps(summary_fee, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"\n[save] {out_dir}/result_raw{suffix}.json, "
          f"summary_fee_zero{suffix}.json, "
          f"summary_fee_{int(args.fee_rate*10000)}bp{suffix}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
