"""エントリポイント。

1 サイクルの制御フロー:
  1. 起動確認
  2. STOP / HALT 確認
  3. 市場データ取得
  4. データ健全性チェック
  5. スコア計算
  6. 売り候補判定
  7. 買い候補判定
  8. ポートフォリオ制約確認
  9. 発注候補の抽出
 10. dry-run 注文の記録
 11. 状態保存
 12. 通知
 13. 異常時は HALT

live 注文は実装しない。order_executor は dry-run 記録専用。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    yaml = None  # type: ignore[assignment]

from market_watcher import MarketSnapshot, MarketWatcher
from notifier import Notifier
from order_executor import OrderExecutor
from regime_filter import load_daily_csv, make_index_trend_filter
from risk_guard import BuyVerdict, Decision, RiskGuard
from scorer import Score, Scorer, apply_cash_bonus
from state_store import Position, StateStore

RegimeGate = Callable[[float], tuple[bool, str]]

logger = logging.getLogger("main")


# ----------------------------------------------------------------------
# 設定読み込み
# ----------------------------------------------------------------------
def load_config() -> dict[str, Any]:
    path = os.environ.get("CONFIG_PATH", "./config/app.yaml")
    p = Path(path)
    if not p.exists():
        logger.error("config not found: %s", path)
        return {}
    if yaml is None:
        logger.error("PyYAML is required. run: pip install -r requirements.txt")
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.exception("failed to parse config: %s", e)
        return {}
    # env の RUN_MODE が yaml の mode を上書き
    mode_env = os.environ.get("RUN_MODE")
    if mode_env:
        cfg["mode"] = mode_env
    return cfg


# ----------------------------------------------------------------------
# dry-run 用の擬似ポートフォリオ評価
# ----------------------------------------------------------------------
def _initial_cash_jpy(cfg: dict[str, Any]) -> float:
    return float((cfg.get("portfolio") or {}).get("initial_cash_jpy", 1_000_000.0))


def compute_equity(
    state: StateStore, snapshot: MarketSnapshot, cfg: dict[str, Any],
) -> tuple[float, float]:
    """(cash, total_equity) を JPY で返す。

    dry-run 用の簡易評価。保有は「記録された size_jpy 分を entry_price で建てた」
    と仮定し、現在価格で時価評価する。
    TODO(live): GMOコイン Private API から残高・建玉を取得する。
    """
    positions = state.positions()
    pos_value = 0.0
    used_cash = 0.0
    for sym, pos in positions.items():
        tk = snapshot.tickers.get(sym)
        current_px = tk.last if tk else pos.entry_price
        if pos.entry_price > 0:
            units = pos.size_jpy / pos.entry_price
            pos_value += units * current_px
        used_cash += pos.size_jpy
    cash = max(_initial_cash_jpy(cfg) - used_cash, 0.0)
    total = cash + pos_value
    return cash, total


# ----------------------------------------------------------------------
# 観察ログ（日次ローテート JSONL + コンソールテーブル）
# ----------------------------------------------------------------------
def _score_log_path() -> Path:
    base = Path(os.environ.get("STATE_DIR", "./data")) / "score_log"
    base.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return base / f"{day}.jsonl"


def _write_cycle_log(record: dict[str, Any]) -> None:
    try:
        path = _score_log_path()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("failed to write cycle log: %s", e)


def _log_eval_table(evals: list[dict[str, Any]]) -> None:
    """銘柄ごとのスコアと verdict を1行で並べる観察用テーブル。"""
    if not evals:
        return
    logger.info(
        "  %-10s %6s %6s %6s %6s %6s %6s %6s %6s %6s  %s",
        "symbol", "total", "trend", "liq", "heat", "vol",
        "dup", "cash", "rule", "ai", "verdict",
    )
    for e in evals:
        logger.info(
            "  %-10s %6.1f %6.1f %6.1f %6.1f %6.1f %6.1f %6.1f %6.1f %6.1f  %s",
            e["symbol"], e["total"], e["trend"], e["liquidity"],
            e["heat"], e["volatility"], e["dup_penalty"], e["cash_bonus"],
            e["rule"], e["ai"], e["verdict"],
        )


def _build_portfolio(
    state: StateStore,
    snapshot: MarketSnapshot,
    sell_decisions: list[Decision],
    cash: float,
    equity: float,
) -> dict[str, Any]:
    positions_list: list[dict[str, Any]] = []
    exit_reasons = {d.symbol: d.reason for d in sell_decisions}
    for sym, pos in state.positions().items():
        tk = snapshot.tickers.get(sym)
        cur = tk.last if tk else pos.entry_price
        pnl_pct = (
            (cur - pos.entry_price) / pos.entry_price * 100.0
            if pos.entry_price > 0 else 0.0
        )
        positions_list.append({
            "symbol": sym,
            "entry_price": pos.entry_price,
            "size_jpy": pos.size_jpy,
            "current_price": cur,
            "pnl_pct": round(pnl_pct, 3),
            "exit_candidate": exit_reasons.get(sym),
        })
    return {
        "cash_jpy": round(cash, 2),
        "equity_jpy": round(equity, 2),
        "cash_ratio": round((cash / equity) if equity > 0 else 0.0, 4),
        "positions": positions_list,
    }


def _build_evaluations(
    scores: list[Score],
    verdicts: list[BuyVerdict],
    portfolio_rejections: dict[str, str],
    buy_decisions: list[Decision],
) -> list[dict[str, Any]]:
    verdict_map = {v.symbol: v for v in verdicts}
    selected = {d.symbol for d in buy_decisions}
    out: list[dict[str, Any]] = []
    for s in scores:
        v = verdict_map.get(s.symbol)
        if s.symbol in selected:
            verdict_str = "selected"
        elif s.symbol in portfolio_rejections:
            verdict_str = f"portfolio:{portfolio_rejections[s.symbol]}"
        elif v is None:
            verdict_str = "n/a"
        elif v.passes:
            verdict_str = "passed_but_not_selected"
        else:
            verdict_str = v.reason
        out.append({
            "symbol": s.symbol,
            "trend": round(s.trend, 2),
            "liquidity": round(s.liquidity, 2),
            "heat": round(s.heat, 2),
            "volatility": round(s.volatility, 2),
            "dup_penalty": round(s.dup_penalty, 2),
            "cash_bonus": round(s.cash_bonus, 2),
            "rule": round(s.rule_score, 2),
            "ai": round(s.ai_score, 2),
            "total": round(s.total, 2),
            "buy_candidate": bool(v and v.passes),
            "strong_buy": bool(v and v.strong),
            "verdict": verdict_str,
        })
    return out


def build_regime_gate(cfg: dict[str, Any]) -> RegimeGate | None:
    """config の regime_filter.* を読んで (ts) -> (allow, reason) を返す関数を作る。

    現状は ndx_trend のみ対応。未設定 or CSV 読み込み失敗時は None（=フィルター無効）。
    """
    rf = (cfg.get("regime_filter") or {})
    if not rf.get("enabled"):
        return None

    gates: list[tuple[str, Callable[[float], bool]]] = []

    ndx = rf.get("ndx_trend") or {}
    if ndx.get("enabled"):
        path = Path(ndx.get("csv_path", "./data/market/NDX_d.csv"))
        if not path.exists():
            logger.warning("regime_filter ndx_trend: csv not found (%s). disabled.", path)
        else:
            try:
                bars = load_daily_csv(path)
                ma_s = int(ndx.get("ma_short", 5))
                ma_l = int(ndx.get("ma_long", 10))
                gates.append(("ndx_trend", make_index_trend_filter(bars, ma_s, ma_l)))
                logger.info("regime_filter ndx_trend enabled ma_short=%d ma_long=%d bars=%d",
                            ma_s, ma_l, len(bars))
            except Exception as e:
                logger.exception("regime_filter ndx_trend: load failed: %s", e)

    if not gates:
        return None

    def gate(ts_utc: float) -> tuple[bool, str]:
        for name, f in gates:
            if not f(ts_utc):
                return False, f"regime_blocked:{name}"
        return True, ""

    return gate


def apply_simulated_fill(state: StateStore, d: Decision, cooldown_min: float) -> None:
    """dry-run 用の擬似約定を state に反映する。実約定ではない。"""
    if d.side == "buy":
        state.set_position(Position(
            symbol=d.symbol,
            size_jpy=d.size_jpy,
            entry_price=d.price_ref,
            entry_ts=time.time(),
            highest_px=d.price_ref,
        ))
    elif d.side == "sell":
        state.remove_position(d.symbol)
        state.set_cooldown(d.symbol, minutes=cooldown_min)
    logger.info("[SIMULATED FILL] %s %s size=%.0f price=%.2f",
                d.side, d.symbol, d.size_jpy, d.price_ref)


# ----------------------------------------------------------------------
# 1 サイクル
# ----------------------------------------------------------------------
def run_cycle(
    cfg: dict[str, Any],
    state: StateStore,
    watcher: MarketWatcher,
    scorer: Scorer,
    guard: RiskGuard,
    executor: OrderExecutor,
    notifier: Notifier,
    regime_gate: RegimeGate | None = None,
) -> None:
    logger.info("---- cycle start ----")
    cycle_start = time.time()
    record: dict[str, Any] = {
        "cycle_ts": cycle_start,
        "iso_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(cycle_start)),
        "halted": False,
        "halt_reason": "",
        "stop_file": False,
        "portfolio": {},
        "evaluations": [],
        "decisions": [],
        "errors": [],
    }

    # 2. HALT チェック
    was_halted = guard.is_halted()
    if was_halted:
        logger.warning("HALT active (%s). skipping cycle.", state.halt_reason())
        record["halted"] = True
        record["halt_reason"] = state.halt_reason()
        _write_cycle_log(record)
        return

    # 3. 市場データ取得
    try:
        snapshot = watcher.fetch()
    except Exception as e:
        logger.exception("market fetch failed: %s", e)
        guard.on_error(e)
        notifier.notify_error(e)
        record["errors"].append(f"fetch: {e}")
        if guard.is_halted():
            record["halted"] = True
            record["halt_reason"] = state.halt_reason()
            notifier.notify_halt(state.halt_reason())
        _write_cycle_log(record)
        return

    # 4. データ健全性
    if not guard.health_check(snapshot):
        logger.error("health check failed → HALT")
        record["halted"] = True
        record["halt_reason"] = state.halt_reason() or "health_check_failed"
        record["errors"].append(f"health_check: {record['halt_reason']}")
        notifier.notify_halt(record["halt_reason"])
        _write_cycle_log(record)
        return

    stop_active = guard.is_stop_file_active()
    record["stop_file"] = stop_active

    regime_allow, regime_reason = (True, "")
    if regime_gate is not None:
        regime_allow, regime_reason = regime_gate(cycle_start)
        record["regime"] = {"allow_buy": regime_allow, "reason": regime_reason}
        if not regime_allow:
            logger.warning("regime filter blocks buys: %s", regime_reason)

    # 5. スコア計算
    try:
        scores = scorer.score(snapshot, held_symbols=state.positions().keys())
        # ポートフォリオ側の後付け: 現金余力ボーナス
        cash, equity = compute_equity(state, snapshot, cfg)
        cash_ratio = (cash / equity) if equity > 0 else 0.0
        scores = apply_cash_bonus(scores, cash_ratio, cfg)
    except Exception as e:
        logger.exception("scoring failed: %s", e)
        guard.on_error(e)
        notifier.notify_error(e)
        record["errors"].append(f"score: {e}")
        if guard.is_halted():
            record["halted"] = True
            record["halt_reason"] = state.halt_reason()
            notifier.notify_halt(state.halt_reason())
        _write_cycle_log(record)
        return

    # 6. 売り候補
    try:
        sell_decisions = guard.evaluate_sells(snapshot)
    except Exception as e:
        logger.exception("sell eval failed: %s", e)
        guard.on_error(e)
        notifier.notify_error(e)
        record["errors"].append(f"sell: {e}")
        _write_cycle_log(record)
        return

    # 7-9. 買い候補 → 制約 → 抽出
    verdicts: list[BuyVerdict] = []
    portfolio_rejections: dict[str, str] = {}
    buy_decisions: list[Decision] = []

    if stop_active:
        logger.warning("STOP file active. buys suppressed (sells still evaluated).")
        verdicts = [
            BuyVerdict(
                s.symbol, False, False,
                "already_held" if state.has_position(s.symbol) else "stop_file",
            )
            for s in scores
        ]
    elif not regime_allow:
        verdicts = [
            BuyVerdict(
                s.symbol, False, False,
                "already_held" if state.has_position(s.symbol) else regime_reason,
            )
            for s in scores
        ]
    else:
        try:
            passed, verdicts = guard.evaluate_buy_candidates(scores)
            buy_decisions, portfolio_rejections = guard.apply_portfolio_constraints(
                passed, snapshot, cash_jpy=cash, total_equity_jpy=equity,
            )
        except Exception as e:
            logger.exception("buy eval failed: %s", e)
            guard.on_error(e)
            notifier.notify_error(e)
            record["errors"].append(f"buy: {e}")
            _write_cycle_log(record)
            return

    # ポートフォリオ・評価表の組み立て
    record["portfolio"] = _build_portfolio(state, snapshot, sell_decisions, cash, equity)
    record["evaluations"] = _build_evaluations(
        scores, verdicts, portfolio_rejections, buy_decisions,
    )
    logger.info("portfolio cash=%.0f equity=%.0f cash_ratio=%.3f",
                cash, equity, record["portfolio"]["cash_ratio"])
    _log_eval_table(record["evaluations"])
    logger.info("decisions sells=%d buys=%d", len(sell_decisions), len(buy_decisions))

    # 10-12. 記録 + 擬似約定 + 通知
    cooldown_min = float((cfg.get("exits", {}) or {}).get("cooldown_min", 180))
    for d in sell_decisions + buy_decisions:
        try:
            executor.execute(d)
            apply_simulated_fill(state, d, cooldown_min)
            notifier.notify_order(d)
            record["decisions"].append({
                "symbol": d.symbol, "side": d.side,
                "size_jpy": d.size_jpy, "price_ref": d.price_ref,
                "reason": d.reason, "strong": d.strong,
            })
        except Exception as e:
            logger.exception("order record failed: %s", e)
            guard.on_error(e)
            notifier.notify_error(e)
            record["errors"].append(f"execute {d.symbol}: {e}")

    # 11. 状態保存
    state.mark_scored()
    state.save()
    guard.on_success()

    # HALT が今サイクルで立った場合の通知
    if guard.is_halted() and not was_halted:
        record["halted"] = True
        record["halt_reason"] = state.halt_reason()
        notifier.notify_halt(state.halt_reason())

    _write_cycle_log(record)
    logger.info("---- cycle end ----")


# ----------------------------------------------------------------------
# メインループ
# ----------------------------------------------------------------------
def run() -> int:
    cfg = load_config()
    if not cfg:
        logger.error("config is empty. abort.")
        return 1

    mode = os.environ.get("RUN_MODE") or cfg.get("mode") or "dry_run"

    # 1. 起動確認
    logger.info("=" * 60)
    logger.info("gmo-bot-safe starting  mode=%s", mode)
    logger.info("symbols core=%s  satellite=%s",
                (cfg.get("symbols") or {}).get("core"),
                (cfg.get("symbols") or {}).get("satellite"))
    logger.info("limits %s", cfg.get("limits"))
    logger.info("=" * 60)

    if mode == "live":
        logger.error(
            "RUN_MODE=live requested, but LIVE IS NOT IMPLEMENTED. "
            "Forcing dry_run behavior. To run live, see CLAUDE.md and DESIGN.md."
        )
        mode = "dry_run"

    state = StateStore()
    watcher = MarketWatcher(cfg)
    scorer = Scorer(cfg)
    guard = RiskGuard(cfg, state)
    executor = OrderExecutor(cfg, mode=mode)
    notifier = Notifier(cfg)
    regime_gate = build_regime_gate(cfg)

    interval = int((cfg.get("loop") or {}).get("score_interval_sec", 900))
    logger.info("loop interval %ds", interval)

    while True:
        try:
            run_cycle(cfg, state, watcher, scorer, guard, executor, notifier, regime_gate)
        except KeyboardInterrupt:
            logger.info("interrupted by user. shutting down.")
            return 0
        except Exception as e:  # セーフティネット（サイクル内の例外は原則個別処理）
            logger.exception("uncaught error in cycle: %s", e)
            try:
                guard.on_error(e)
                notifier.notify_error(e)
            except Exception:
                logger.exception("error while handling error")

        logger.info("sleeping %ds", interval)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("interrupted during sleep. shutting down.")
            return 0


def _setup_logging(cfg: dict[str, Any]) -> None:
    """stderr + ファイル（日次ローテート）のハンドラをセットアップする。"""
    log_cfg = cfg.get("logging", {}) or {}
    level = os.environ.get("LOG_LEVEL", log_cfg.get("level", "INFO"))
    log_dir = Path(os.environ.get("LOG_DIR", log_cfg.get("dir", "./logs")))
    log_dir.mkdir(parents=True, exist_ok=True)
    backup_count = int(log_cfg.get("backup_count", 14))
    fmt = "%(asctime)s %(levelname)s %(name)s  %(message)s"

    root = logging.getLogger()
    root.setLevel(level)
    # 既存ハンドラを一掃（再起動時の二重登録防止）
    for h in list(root.handlers):
        root.removeHandler(h)

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(logging.Formatter(fmt))
    root.addHandler(ch)

    fh = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_dir / "bot.log"),
        when="midnight",
        backupCount=backup_count,
        encoding="utf-8",
        utc=True,
    )
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)

    # API キー・シークレットが何かの拍子に log record に乗っても出力前に伏字化する。
    # filter は handler 個別に attach（root 直付けは複数 handler を経由するときに
    # 二重置換になり得るため避ける）。
    from log_filters import SecretMaskFilter
    _secrets = [
        os.environ.get("GMO_API_KEY", ""),
        os.environ.get("GMO_API_SECRET", ""),
    ]
    if any(_secrets):
        _mask = SecretMaskFilter(_secrets)
        ch.addFilter(_mask)
        fh.addFilter(_mask)


if __name__ == "__main__":
    _pre_cfg = load_config()
    _setup_logging(_pre_cfg)
    sys.exit(run())
