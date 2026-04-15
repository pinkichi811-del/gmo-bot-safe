"""ローカル大量試行最適化の worker 共通モジュール。

ProcessPoolExecutor (spawn) で各 worker にロードされる純関数群。
親プロセスで事前ロードした candles/ts_list/ndx_bars を initializer 経由で受け取り、
worker 内の global dict に precompute_extras と ndx filter をキャッシュする。
"""
from __future__ import annotations

import math
import statistics
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from backtest_v1_tf import analyze, precompute_extras  # noqa: E402
from backtest_regime import run_bt_with_filters  # noqa: E402
from regime_filter import (  # noqa: E402
    make_index_trend_filter, make_vix_filter, make_event_avoidance_filter,
    filter_us_regular_only,
)


# ---------------------------------------------------------------------------
# Worker global state (ProcessPool spawn 対応)
# ---------------------------------------------------------------------------
# 多銘柄対応: シンボル別の candles/closes/ts_list を辞書で保持
_SYM_CANDLES: dict[str, list] = {}
_SYM_CLOSES: dict[str, list] = {}
_SYM_TS_LIST: dict[str, list[float]] = {}
# 後方互換エイリアス (BTC 単一用: 旧 API で参照されている場所に対応)
_CANDLES: list = []
_CLOSES: list = []
_TS_LIST: list[float] = []
_NDX_BARS: list = []
_SPX_BARS: list = []
_VIX_BARS: list = []
_EVENTS: list = []
# LRU caches (OrderedDict + maxsize)
_EXTRAS_CACHE: OrderedDict = OrderedDict()  # key = (sym, ma_s, ma_l)
_NDX_FILTER_CACHE: OrderedDict = OrderedDict()
_SPX_FILTER_CACHE: OrderedDict = OrderedDict()
_VIX_FILTER_CACHE: OrderedDict = OrderedDict()
_EVENTS_FILTER_CACHE: OrderedDict = OrderedDict()
_EXTRAS_MAX = 24  # 多銘柄で倍必要
_NDX_MAX = 9
_SPX_MAX = 9


def _init_worker(candles, ts_list, ndx_bars, spx_bars, vix_bars, events,
                 sym_data: dict | None = None) -> None:
    """
    sym_data: {'BTC': (candles, ts_list), 'ETH': (candles, ts_list), ...}
              None の場合は (candles, ts_list) を BTC として登録
    後方互換: 旧呼び出し (単一 candles) も動く。
    """
    global _CANDLES, _CLOSES, _TS_LIST, _NDX_BARS, _SPX_BARS, _VIX_BARS, _EVENTS
    global _SYM_CANDLES, _SYM_CLOSES, _SYM_TS_LIST
    global _EXTRAS_CACHE, _NDX_FILTER_CACHE, _SPX_FILTER_CACHE
    global _VIX_FILTER_CACHE, _EVENTS_FILTER_CACHE
    if sym_data is None:
        sym_data = {"BTC": (candles, ts_list)}
    _SYM_CANDLES = {s: cs[0] for s, cs in sym_data.items()}
    _SYM_CLOSES = {s: [c.close for c in cs[0]] for s, cs in sym_data.items()}
    _SYM_TS_LIST = {s: cs[1] for s, cs in sym_data.items()}
    # 後方互換: BTC を単一エイリアスに
    btc = sym_data.get("BTC", (candles, ts_list))
    _CANDLES = btc[0]
    _CLOSES = _SYM_CLOSES.get("BTC", [])
    _TS_LIST = btc[1]
    _NDX_BARS = ndx_bars
    _SPX_BARS = spx_bars
    _VIX_BARS = vix_bars
    _EVENTS = events
    _EXTRAS_CACHE = OrderedDict()
    _NDX_FILTER_CACHE = OrderedDict()
    _SPX_FILTER_CACHE = OrderedDict()
    _VIX_FILTER_CACHE = OrderedDict()
    _EVENTS_FILTER_CACHE = OrderedDict()


def _iso_ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


PERIODS_4: dict[str, tuple[float, float]] = {
    "train_extra": (_iso_ts("2022-01-01T00:00:00Z"), _iso_ts("2023-12-31T23:45:00Z")),
    "train": (_iso_ts("2024-01-01T00:00:00Z"), _iso_ts("2025-06-30T23:45:00Z")),
    "val": (_iso_ts("2025-07-01T00:00:00Z"), _iso_ts("2025-12-31T23:45:00Z")),
    "final": (_iso_ts("2026-01-01T00:00:00Z"), _iso_ts("2026-03-31T23:45:00Z")),
}
PERIOD_NAMES = ("train_extra", "train", "val", "final")


# ---------------------------------------------------------------------------
# Cached lookups
# ---------------------------------------------------------------------------
def _lru_get_or_build(cache: OrderedDict, key, maxsize: int, builder):
    if key in cache:
        cache.move_to_end(key)
        return cache[key]
    val = builder()
    cache[key] = val
    if len(cache) > maxsize:
        cache.popitem(last=False)
    return val


def get_extras_cached(ma_short: int, ma_long: int, symbol: str = "BTC") -> dict:
    return _lru_get_or_build(
        _EXTRAS_CACHE, (symbol, ma_short, ma_long), _EXTRAS_MAX,
        lambda: precompute_extras(_SYM_CANDLES.get(symbol, _CANDLES),
                                  ma_short, ma_long),
    )


def get_ndx_filter_cached(ma_short: int, ma_long: int) -> Callable[[float], bool]:
    return _lru_get_or_build(
        _NDX_FILTER_CACHE, (ma_short, ma_long), _NDX_MAX,
        lambda: make_index_trend_filter(_NDX_BARS, ma_short=ma_short, ma_long=ma_long),
    )


def get_spx_filter_cached(ma_short: int, ma_long: int) -> Callable[[float], bool]:
    return _lru_get_or_build(
        _SPX_FILTER_CACHE, (ma_short, ma_long), _SPX_MAX,
        lambda: make_index_trend_filter(_SPX_BARS, ma_short=ma_short, ma_long=ma_long),
    )


def get_vix_filter_cached(vix_max: float) -> Callable[[float], bool]:
    return _lru_get_or_build(
        _VIX_FILTER_CACHE, vix_max, 4,
        lambda: make_vix_filter(_VIX_BARS, max_vix=vix_max),
    )


def get_events_filter_cached(window_min: int) -> Callable[[float], bool]:
    return _lru_get_or_build(
        _EVENTS_FILTER_CACHE, window_min, 2,
        lambda: make_event_avoidance_filter(_EVENTS, window_min, window_min),
    )


# ---------------------------------------------------------------------------
# Patched run_bt_with_filters: precomputed extras を注入
# ---------------------------------------------------------------------------
import bisect  # noqa: E402
from collections import Counter  # noqa: E402


def _run_bt_multi_cached(
    symbols: list[str], period, buy_trend, ma_short, ma_long,
    tp_pct, sl_pct, filters,
    max_hold_bars: int = 0,
    trail_pct: float = 0.0,
    cooldown_min: int = 0,
    max_positions: int = 1,
    fee_rate: float = 0.0005,
    per_trade_jpy: float = 10_000.0, initial_cash: float = 1_000_000.0,
) -> dict[str, Any]:
    """多銘柄対応バックテスト。symbols=[BTC] でも動く (単一銘柄)。"""
    # 前準備: 各シンボルの trend/heat/closes
    sym_ctx = {}
    for sym in symbols:
        if sym not in _SYM_CANDLES:
            continue
        extras = get_extras_cached(ma_short, ma_long, symbol=sym)
        sym_ctx[sym] = {
            "closes": _SYM_CLOSES[sym], "ts_list": _SYM_TS_LIST[sym],
            "trend": extras["trend"], "heat": extras["heat"],
        }
    if not sym_ctx:
        # 参照データ無し: 空で返す
        return {"trades": [], "equity_curve": [],
                "final_unrealized_jpy": 0.0,
                "initial_cash": initial_cash, "period": period,
                "raw_signals": 0, "filter_skips": {}}

    start, end = period
    # 各シンボルの開始/終了インデックスを求め、時刻順にイベントをマージ
    events_list: list[tuple[float, str, int]] = []
    for sym, ctx in sym_ctx.items():
        tsl = ctx["ts_list"]
        i_start = max(ma_long + 5, bisect.bisect_left(tsl, start))
        i_end = bisect.bisect_right(tsl, end)
        for i in range(i_start, i_end):
            events_list.append((tsl[i], sym, i))
    events_list.sort(key=lambda x: x[0])

    trades: list[dict] = []
    equity_curve: list[tuple[float, float]] = []
    positions: dict[str, dict | None] = {s: None for s in sym_ctx}
    last_exit_ts: dict[str, float] = {s: -1e18 for s in sym_ctx}
    realized_net = 0.0
    filter_skips: Counter = Counter()
    raw_signals = 0

    eq_sample_every = max(12, 12 * len(sym_ctx))  # 銘柄多いとサンプル間隔を広く

    for idx, (cur_ts, sym, i) in enumerate(events_list):
        ctx = sym_ctx[sym]
        cur_close = ctx["closes"][i]
        trend_i = ctx["trend"][i]
        heat_i = ctx["heat"][i]
        pos = positions[sym]

        if pos is None:
            # 空きポジ数 (他シンボル含む)
            open_count = sum(1 for p in positions.values() if p is not None)
            if (open_count < max_positions and
                    trend_i >= buy_trend and heat_i >= -8.0 and
                    (60.0 + trend_i + heat_i + 5.0) >= 70.0):
                raw_signals += 1
                if cooldown_min > 0 and (cur_ts - last_exit_ts[sym]) < cooldown_min * 60:
                    filter_skips[f"cooldown:{sym}"] += 1
                else:
                    passed = True
                    for fname, fn in filters:
                        if not fn(cur_ts):
                            filter_skips[fname] += 1
                            passed = False
                            break
                    if passed:
                        positions[sym] = {
                            "entry_price": cur_close, "entry_ts": cur_ts,
                            "bars_held": 0, "max_close": cur_close, "symbol": sym,
                        }
        else:
            pos["bars_held"] += 1
            if cur_close > pos["max_close"]:
                pos["max_close"] = cur_close
            pct = (cur_close - pos["entry_price"]) / pos["entry_price"] * 100.0
            reason = None
            if pct <= sl_pct:
                reason = "stop_loss"
            elif trail_pct > 0.0:
                max_pct = (pos["max_close"] - pos["entry_price"]) / pos["entry_price"] * 100.0
                if max_pct >= 1.0:
                    drop_from_peak = (cur_close - pos["max_close"]) / pos["max_close"] * 100.0
                    if drop_from_peak <= -trail_pct:
                        reason = "trailing_stop"
            if reason is None and pct >= tp_pct:
                reason = "take_profit"
            if reason is None and max_hold_bars > 0 and pos["bars_held"] >= max_hold_bars:
                reason = "max_hold"
            if reason is not None:
                gross = pct / 100.0 * per_trade_jpy
                fee = per_trade_jpy * fee_rate + (per_trade_jpy + gross) * fee_rate
                net = gross - fee
                trades.append({
                    "symbol": sym,
                    "entry_ts": pos["entry_ts"], "exit_ts": cur_ts,
                    "entry_price": pos["entry_price"], "exit_price": cur_close,
                    "bars_held": pos["bars_held"],
                    "exit_reason": reason,
                    "gross_pnl_jpy": gross, "fee_jpy": fee, "net_pnl_jpy": net,
                })
                realized_net += net
                last_exit_ts[sym] = cur_ts
                positions[sym] = None

        if idx % eq_sample_every == 0:
            unr = 0.0
            for p in positions.values():
                if p is not None:
                    # 未実現は各シンボルの現在価格がわからないので簡略化: 0 とする
                    pass
            equity_curve.append((cur_ts, initial_cash + realized_net + unr))

    final_unr = 0.0
    for sym, p in positions.items():
        if p is not None:
            ctx = sym_ctx[sym]
            final_close = ctx["closes"][bisect.bisect_right(ctx["ts_list"], end) - 1]
            final_unr += (final_close - p["entry_price"]) / p["entry_price"] * per_trade_jpy

    return {
        "trades": trades, "equity_curve": equity_curve,
        "final_unrealized_jpy": final_unr,
        "initial_cash": initial_cash, "period": period,
        "raw_signals": raw_signals,
        "filter_skips": dict(filter_skips),
    }


def _run_bt_cached(
    candles, ts_list, period, buy_trend, ma_short, ma_long,
    tp_pct, sl_pct, filters,
    max_hold_bars: int = 0,   # 0 = 無制限
    trail_pct: float = 0.0,    # 0 = 無効
    cooldown_min: int = 0,     # 0 = 無効
    fee_rate: float = 0.0005,
    per_trade_jpy: float = 10_000.0, initial_cash: float = 1_000_000.0,
) -> dict[str, Any]:
    """run_bt_with_filters を extras キャッシュ対応にした版。
    trail_pct: max_close_since_entry × (1 - trail_pct/100) を下回れば exit
              ただし max_close が entry × 1.01 以上になってから有効化 (早期 noise 排除)
    cooldown_min: 直近 exit から X 分未満は再エントリー不可
    max_hold_bars: bars_held >= X で強制 exit (0 = 無制限)
    """
    extras = get_extras_cached(ma_short, ma_long)
    trend = extras["trend"]
    heat = extras["heat"]
    closes = _CLOSES if _CLOSES else [c.close for c in candles]

    start, end = period
    i_start = max(ma_long + 5, bisect.bisect_left(ts_list, start))
    i_end = bisect.bisect_right(ts_list, end)

    trades: list[dict] = []
    equity_curve: list[tuple[float, float]] = []
    pos: dict | None = None
    realized_net = 0.0
    filter_skips: Counter = Counter()
    raw_signals = 0
    last_exit_ts = -1e18  # cooldown 判定

    for i in range(i_start, i_end):
        cur_close = closes[i]
        cur_ts = candles[i].ts

        if pos is None:
            if (trend[i] >= buy_trend and heat[i] >= -8.0 and
                    (60.0 + trend[i] + heat[i] + 5.0) >= 70.0):
                raw_signals += 1
                # cooldown check
                if cooldown_min > 0 and (cur_ts - last_exit_ts) < cooldown_min * 60:
                    filter_skips["cooldown"] += 1
                else:
                    passed = True
                    for name, fn in filters:
                        if not fn(cur_ts):
                            filter_skips[name] += 1
                            passed = False
                            break
                    if passed:
                        pos = {
                            "entry_price": cur_close, "entry_ts": cur_ts,
                            "bars_held": 0, "max_close": cur_close,
                        }
        else:
            pos["bars_held"] += 1
            if cur_close > pos["max_close"]:
                pos["max_close"] = cur_close
            pct = (cur_close - pos["entry_price"]) / pos["entry_price"] * 100.0
            reason = None
            # 優先度: SL → trail → TP → max_hold
            if pct <= sl_pct:
                reason = "stop_loss"
            elif trail_pct > 0.0:
                max_pct = (pos["max_close"] - pos["entry_price"]) / pos["entry_price"] * 100.0
                if max_pct >= 1.0:
                    drop_from_peak = (cur_close - pos["max_close"]) / pos["max_close"] * 100.0
                    if drop_from_peak <= -trail_pct:
                        reason = "trailing_stop"
            if reason is None and pct >= tp_pct:
                reason = "take_profit"
            if reason is None and max_hold_bars > 0 and pos["bars_held"] >= max_hold_bars:
                reason = "max_hold"
            if reason is not None:
                gross = pct / 100.0 * per_trade_jpy
                fee = per_trade_jpy * fee_rate + (per_trade_jpy + gross) * fee_rate
                net = gross - fee
                trades.append({
                    "entry_ts": pos["entry_ts"], "exit_ts": cur_ts,
                    "entry_price": pos["entry_price"], "exit_price": cur_close,
                    "bars_held": pos["bars_held"],
                    "exit_reason": reason,
                    "gross_pnl_jpy": gross, "fee_jpy": fee, "net_pnl_jpy": net,
                })
                realized_net += net
                last_exit_ts = cur_ts
                pos = None

        if i % 12 == 0:
            unr = 0.0
            if pos is not None:
                unr = (cur_close - pos["entry_price"]) / pos["entry_price"] * per_trade_jpy
            equity_curve.append((cur_ts, initial_cash + realized_net + unr))

    final_unr = 0.0
    if pos is not None:
        final_close = closes[i_end - 1]
        final_unr = (final_close - pos["entry_price"]) / pos["entry_price"] * per_trade_jpy

    return {
        "trades": trades, "equity_curve": equity_curve,
        "final_unrealized_jpy": final_unr,
        "initial_cash": initial_cash, "period": period,
        "raw_signals": raw_signals,
        "filter_skips": dict(filter_skips),
    }


# ---------------------------------------------------------------------------
# Composite score (4 期間統合)
# ---------------------------------------------------------------------------
def composite4(
    per_period: dict[str, dict], alpha: float = 0.5, beta: float = 0.4,
    gamma: float = 0.3, min_trades: int = 10, pf_cap: float = 5.0,
) -> dict[str, float]:
    pfs: list[float] = []
    dds: list[float] = []
    trade_guard = 0
    for name in PERIOD_NAMES:
        st = per_period.get(name, {})
        pf_raw = st.get("profit_factor", 0.0)
        if pf_raw == float("inf") or pf_raw > pf_cap:
            pf = pf_cap
        elif pf_raw != pf_raw:  # NaN
            pf = 0.0
        else:
            pf = max(0.0, pf_raw)
        pfs.append(pf)
        dds.append(st.get("max_drawdown_pct", 0.0))
        if st.get("trades", 0) < min_trades:
            trade_guard += 1

    pf_mean = statistics.mean(pfs)
    pf_std = statistics.pstdev(pfs) if len(pfs) > 1 else 0.0
    dd_max = max(dds) if dds else 0.0

    pf_train = per_period.get("train", {}).get("profit_factor", 0.0)
    pf_val = per_period.get("val", {}).get("profit_factor", 0.0)
    if pf_train == float("inf"): pf_train = pf_cap
    if pf_val == float("inf"): pf_val = pf_cap
    spread = abs(pf_train - pf_val)

    composite = (
        pf_mean
        - alpha * pf_std
        - beta * (dd_max / 100.0)
        - gamma * spread
        - 1.0 * trade_guard
    )
    return {
        "pf_mean": pf_mean, "pf_std": pf_std, "dd_max": dd_max,
        "pf_train_val_spread": spread, "trade_guard": trade_guard,
        "composite": composite,
    }


# ---------------------------------------------------------------------------
# Param utilities
# ---------------------------------------------------------------------------
def param_key(p: dict) -> str:
    syms = "+".join(sorted(p.get("symbols") or ["BTC"]))
    return (
        f"sym{syms}_mp{p.get('max_positions', 1)}_"
        f"t{p['buy_trend']}_s{p['ma_short']}_l{p['ma_long']}_"
        f"tp{p['tp_pct']:.1f}_sl{p['sl_pct']:.1f}_"
        f"mh{p.get('max_hold_bars', 0)}_tr{p.get('trail_pct', 0.0):.1f}_"
        f"cd{p.get('cooldown_min', 0)}_"
        f"ndx{int(p['use_ndx'])}_ns{p.get('ndx_ma_short') or 0}_nl{p.get('ndx_ma_long') or 0}_"
        f"spx{int(p.get('use_spx', False))}_"
        f"ss{p.get('spx_ma_short') or 0}_sl2{p.get('spx_ma_long') or 0}_"
        f"vix{int(p.get('use_vix', False))}_vm{p.get('vix_max') or 0}_"
        f"ush{int(p.get('use_us_hours', False))}_"
        f"ev{int(p.get('use_events', False))}_ew{p.get('events_window_min') or 0}"
    )


# ---------------------------------------------------------------------------
# Trial entry point
# ---------------------------------------------------------------------------
def _build_filters(param: dict) -> list[tuple[str, Callable[[float], bool]]]:
    filters: list[tuple[str, Callable[[float], bool]]] = []
    if param.get("use_ndx"):
        filters.append(("ndx_trend", get_ndx_filter_cached(
            param["ndx_ma_short"], param["ndx_ma_long"])))
    if param.get("use_spx"):
        filters.append(("spx_trend", get_spx_filter_cached(
            param["spx_ma_short"], param["spx_ma_long"])))
    if param.get("use_vix"):
        filters.append(("vix", get_vix_filter_cached(param["vix_max"])))
    if param.get("use_us_hours"):
        filters.append(("us_hours", filter_us_regular_only))
    if param.get("use_events"):
        filters.append(("events", get_events_filter_cached(
            param["events_window_min"])))
    return filters


def _run_trial(param: dict, alpha: float = 0.5, beta: float = 0.4,
               gamma: float = 0.3) -> dict:
    t0 = time.time()
    try:
        filters = _build_filters(param)
        symbols = param.get("symbols") or ["BTC"]
        max_positions = int(param.get("max_positions", 1))

        per_period: dict[str, dict] = {}
        for pname in PERIOD_NAMES:
            res = _run_bt_multi_cached(
                symbols, PERIODS_4[pname],
                buy_trend=param["buy_trend"],
                ma_short=param["ma_short"], ma_long=param["ma_long"],
                tp_pct=param["tp_pct"], sl_pct=param["sl_pct"],
                filters=filters,
                max_hold_bars=param.get("max_hold_bars", 0),
                trail_pct=param.get("trail_pct", 0.0),
                cooldown_min=param.get("cooldown_min", 0),
                max_positions=max_positions,
            )
            st = analyze(res, PERIODS_4[pname])
            per_period[pname] = st

        comp = composite4(per_period, alpha=alpha, beta=beta, gamma=gamma)

        row: dict[str, Any] = {
            "trial_id": param["trial_id"],
            "stage": param.get("stage", 1),
            "symbols": "+".join(sorted(param.get("symbols") or ["BTC"])),
            "max_positions": max_positions,
            "buy_trend": param["buy_trend"],
            "ma_short": param["ma_short"],
            "ma_long": param["ma_long"],
            "tp_pct": param["tp_pct"],
            "sl_pct": param["sl_pct"],
            "max_hold_bars": param.get("max_hold_bars", 0),
            "trail_pct": param.get("trail_pct", 0.0),
            "cooldown_min": param.get("cooldown_min", 0),
            "use_ndx": int(param.get("use_ndx", False)),
            "ndx_ma_short": param.get("ndx_ma_short") or 0,
            "ndx_ma_long": param.get("ndx_ma_long") or 0,
            "use_spx": int(param.get("use_spx", False)),
            "spx_ma_short": param.get("spx_ma_short") or 0,
            "spx_ma_long": param.get("spx_ma_long") or 0,
            "use_vix": int(param.get("use_vix", False)),
            "vix_max": param.get("vix_max") or 0,
            "use_us_hours": int(param.get("use_us_hours", False)),
            "use_events": int(param.get("use_events", False)),
            "events_window_min": param.get("events_window_min") or 0,
        }
        for pname in PERIOD_NAMES:
            st = per_period[pname]
            pf = st["profit_factor"]
            pf = 999.99 if pf == float("inf") else round(pf, 4)
            row[f"{pname}_trades"] = st["trades"]
            row[f"{pname}_pf"] = pf
            row[f"{pname}_dd"] = round(st["max_drawdown_pct"], 4)
            row[f"{pname}_net_pct"] = round(st["total_pnl_net_pct"], 4)
            row[f"{pname}_wr"] = round(st["win_rate_pct"], 2)
        row["pf_mean"] = round(comp["pf_mean"], 4)
        row["pf_std"] = round(comp["pf_std"], 4)
        row["dd_max"] = round(comp["dd_max"], 4)
        row["pf_spread"] = round(comp["pf_train_val_spread"], 4)
        row["trade_guard"] = comp["trade_guard"]
        row["composite"] = round(comp["composite"], 6)
        row["wall_sec"] = round(time.time() - t0, 3)
        row["status"] = "ok"
        row["error_msg"] = ""
        return row
    except Exception as e:  # noqa: BLE001
        return {
            "trial_id": param.get("trial_id", -1),
            "stage": param.get("stage", 1),
            "symbols": "+".join(sorted(param.get("symbols") or ["BTC"])),
            "max_positions": int(param.get("max_positions", 1)),
            "buy_trend": param.get("buy_trend", 0),
            "ma_short": param.get("ma_short", 0),
            "ma_long": param.get("ma_long", 0),
            "tp_pct": param.get("tp_pct", 0.0),
            "sl_pct": param.get("sl_pct", 0.0),
            "max_hold_bars": param.get("max_hold_bars", 0),
            "trail_pct": param.get("trail_pct", 0.0),
            "cooldown_min": param.get("cooldown_min", 0),
            "use_ndx": int(param.get("use_ndx", False)),
            "ndx_ma_short": param.get("ndx_ma_short") or 0,
            "ndx_ma_long": param.get("ndx_ma_long") or 0,
            "use_spx": int(param.get("use_spx", False)),
            "spx_ma_short": param.get("spx_ma_short") or 0,
            "spx_ma_long": param.get("spx_ma_long") or 0,
            "use_vix": int(param.get("use_vix", False)),
            "vix_max": param.get("vix_max") or 0,
            "use_us_hours": int(param.get("use_us_hours", False)),
            "use_events": int(param.get("use_events", False)),
            "events_window_min": param.get("events_window_min") or 0,
            "composite": -9999.0,
            "wall_sec": round(time.time() - t0, 3),
            "status": "error",
            "error_msg": str(e)[:200],
        }


# CSV 列順序（固定・拡張版）
CSV_COLUMNS = [
    "trial_id", "stage",
    "symbols", "max_positions",
    "buy_trend", "ma_short", "ma_long", "tp_pct", "sl_pct",
    "max_hold_bars", "trail_pct", "cooldown_min",
    "use_ndx", "ndx_ma_short", "ndx_ma_long",
    "use_spx", "spx_ma_short", "spx_ma_long",
    "use_vix", "vix_max",
    "use_us_hours", "use_events", "events_window_min",
]
for _p in PERIOD_NAMES:
    CSV_COLUMNS += [f"{_p}_trades", f"{_p}_pf", f"{_p}_dd",
                    f"{_p}_net_pct", f"{_p}_wr"]
CSV_COLUMNS += [
    "pf_mean", "pf_std", "dd_max", "pf_spread", "trade_guard",
    "composite", "wall_sec", "status", "error_msg",
]


