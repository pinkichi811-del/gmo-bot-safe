"""発注実行。

現段階は **絶対に実注文を出さない**。
dry-run 時は `data/dry_run_orders.jsonl` と `.csv` に記録するのみ。

live 発注は三段ゲートで塞いでいる:
  1. `ENABLE_LIVE_ORDER`（このファイル内の定数・コードゲート）
  2. `LIVE_OK` 環境変数（runtime ゲート・`yes` でのみ通過）
  3. 実装そのものが未着手（`_send_live_order` が NotImplementedError を返す）

live を有効化するには全部のゲートを通す必要がある。単独での有効化は設計上できない。

Phase 4 (2026-05): `_send_live_order_impl` に実装本体を書く。`_send_live_order`
自体は **依然 NotImplementedError を返したまま** であり、gate3 物理ガードは維持。
Phase 5 で単独 PR が `_send_live_order` を `_impl` を呼ぶ形に書き換える設計。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from risk_guard import Decision

if TYPE_CHECKING:  # pragma: no cover
    from gmo_order_client import GmoOrderClient
    from risk_guard import RiskGuard

logger = logging.getLogger(__name__)


# ======================================================================
# 1段目: コードゲート
# ----------------------------------------------------------------------
# この定数を True に変える作業は、単独 PR で、レビューを経て行うこと。
# .env や CLI からは変更できない。
# ======================================================================
ENABLE_LIVE_ORDER: bool = False


# ======================================================================
# 2段目のキー: 環境変数名
# ----------------------------------------------------------------------
# LIVE_OK=yes の時のみ通過。`.env.example` 上は no で固定。
# ======================================================================
LIVE_OK_ENV = "LIVE_OK"


class OrderExecutor:
    def __init__(
        self,
        cfg: dict[str, Any],
        mode: str = "dry_run",
        *,
        order_client: "GmoOrderClient | None" = None,
        risk_guard: "RiskGuard | None" = None,
        sleep_fn: Any = None,
    ) -> None:
        self.cfg = cfg
        self.mode = mode
        self._order_client = order_client
        self._risk_guard = risk_guard
        # ポーリング sleep を inject 可能に (テストで no-op に差し替えられる)
        self._sleep_fn = sleep_fn if sleep_fn is not None else time.sleep

        state_dir = Path(os.environ.get("STATE_DIR", "./data"))
        state_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path: Path = state_dir / "dry_run_orders.jsonl"
        self.csv_path: Path = state_dir / "dry_run_orders.csv"
        self.live_jsonl_path: Path = state_dir / "live_orders.jsonl"
        self.live_csv_path: Path = state_dir / "live_orders.csv"

        if not self.csv_path.exists():
            self._write_csv_header()
        if not self.live_csv_path.exists():
            self._write_live_csv_header()

        order_cfg = cfg.get("order") or {}
        self._min_size_by_symbol: dict[str, float] = {
            k: float(v) for k, v in (order_cfg.get("min_size_by_symbol") or {}).items()
        }
        self._size_decimals_by_symbol: dict[str, int] = {
            k: int(v) for k, v in (order_cfg.get("size_decimals") or {}).items()
        }
        poll_cfg = (order_cfg.get("executions_poll") or {})
        self._poll_max_attempts: int = int(poll_cfg.get("max_attempts", 5))
        self._poll_interval_sec: float = float(poll_cfg.get("interval_sec", 2.0))

        if mode == "live":
            self._log_live_gate_status()

    # ------------------------------------------------------------------
    # gate 状態の可視化
    # ------------------------------------------------------------------
    def _log_live_gate_status(self) -> None:
        logger.error("=" * 60)
        logger.error("mode=live が要求されましたが、live 発注は封印されています。")
        logger.error("  gate1 ENABLE_LIVE_ORDER = %s  (code gate)", ENABLE_LIVE_ORDER)
        logger.error("  gate2 %s = %r  (env gate, need 'yes')",
                     LIVE_OK_ENV, os.environ.get(LIVE_OK_ENV, "no"))
        logger.error("  gate3 _send_live_order は未実装 (NotImplementedError)")
        logger.error("すべての注文は下記ステータスで記録され、送信されません。")
        logger.error("=" * 60)

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def execute(self, decision: Decision) -> dict[str, Any]:
        if self.mode == "live":
            return self._live_execute(decision)
        return self._record_dry_run(decision)

    # ------------------------------------------------------------------
    # live 側: 三段ゲート
    # ------------------------------------------------------------------
    def _live_execute(self, d: Decision) -> dict[str, Any]:
        # gate 1: code
        if not ENABLE_LIVE_ORDER:
            logger.error(
                "[BLOCKED:code_gate] live order rejected for %s %s (ENABLE_LIVE_ORDER=False)",
                d.symbol, d.side,
            )
            return {"status": "blocked_by_code_gate", "symbol": d.symbol}

        # gate 2: env
        if os.environ.get(LIVE_OK_ENV, "no") != "yes":
            logger.error(
                "[BLOCKED:env_gate] live order rejected for %s %s (%s != yes)",
                d.symbol, d.side, LIVE_OK_ENV,
            )
            return {"status": "blocked_by_env_gate", "symbol": d.symbol}

        # gate 3: 実装
        try:
            return self._send_live_order(d)
        except NotImplementedError:
            logger.error(
                "[BLOCKED:not_implemented] live order not coded yet: %s %s",
                d.symbol, d.side,
            )
            return {"status": "not_implemented", "symbol": d.symbol}

    def _send_live_order(self, d: Decision) -> dict[str, Any]:
        """実注文送信のエントリ。**Phase 5 まで NotImplementedError のまま**。

        Phase 4 で実装本体 `_send_live_order_impl` を別途用意したが、本関数は
        CLAUDE.md Hard Rule #1 (gate3 = 実装ゲート) の物理保証として raise を
        残す。Phase 5 の単独 PR で `return self._send_live_order_impl(d)` に
        書き換えるまで、live 注文の経路は実体としてここで遮断される。

        既存テスト `tests/test_smoke.py::test_live_blocked_by_not_implemented`
        が本挙動を assert している。本関数は触らないこと。
        """
        raise NotImplementedError("live order sending is not implemented")

    # ------------------------------------------------------------------
    # 実装本体 (Phase 4): gate3 が開いた Phase 5 で _send_live_order が呼ぶ
    # ------------------------------------------------------------------
    def _send_live_order_impl(self, d: Decision) -> dict[str, Any]:
        """live 注文の実装本体。**現段階では `_send_live_order` から呼ばれない**。

        テスト経由および Phase 5 の単独 PR 適用後にのみ実行される。

        フロー:
          1. size_jpy / price_ref → size_crypto 変換 (config の size_decimals で切り捨て)
          2. 最小ロット未満なら送信せず ``below_min_size`` で返す
          3. `GmoOrderClient.place_market_order` を呼ぶ
          4. order_id を抽出し ``data/live_orders.jsonl`` に記録
          5. `GmoApiError` は catch して ``live_order_error`` で返す
          6. 想定外の例外は呼び出し側へ伝播 (HALT 判定は呼び出し側に任せる)

        部分約定の確認や reject 連発の HALT 判定は Phase 4b / 4c で追加する。
        """
        if self._order_client is None:
            return {
                "status": "no_order_client",
                "symbol": d.symbol,
                "error": "GmoOrderClient is not injected",
            }
        if d.side not in ("buy", "sell"):
            return {
                "status": "invalid_side",
                "symbol": d.symbol,
                "error": f"side must be buy or sell, got {d.side!r}",
            }
        if d.price_ref <= 0:
            return {
                "status": "invalid_price",
                "symbol": d.symbol,
                "error": f"price_ref must be positive, got {d.price_ref}",
            }

        size_crypto = d.size_jpy / d.price_ref
        size_decimals = self._size_decimals_by_symbol.get(d.symbol)
        min_size = self._min_size_by_symbol.get(d.symbol)
        if size_decimals is None or min_size is None:
            return {
                "status": "unknown_symbol",
                "symbol": d.symbol,
                "error": f"size config missing for {d.symbol}",
            }

        # 最小ロット未満は送信しない (config を切り捨てた結果 0 になるケースも含む)
        factor = 10 ** size_decimals
        size_truncated = int(size_crypto * factor) / factor
        if size_truncated < min_size:
            result = {
                "status": "below_min_size",
                "symbol": d.symbol,
                "side": d.side,
                "size_jpy": d.size_jpy,
                "size_crypto": size_truncated,
                "min_size": min_size,
            }
            self._record_live_order(d, status="below_min_size",
                                    size_crypto=size_truncated, order_id="")
            logger.warning(
                "[LIVE BLOCKED] %s %s size_crypto=%.8f < min=%.8f",
                d.symbol, d.side, size_truncated, min_size,
            )
            return result

        # ここから先で GMO API を実際に叩く
        from gmo_api_client import GmoApiError  # 遅延 import (テスト容易性)

        try:
            api_result = self._order_client.place_market_order(
                symbol=d.symbol,
                side=d.side,  # GmoOrderClient 側で BUY/SELL に upper する
                size_crypto=size_crypto,
                size_decimals=size_decimals,
            )
        except GmoApiError as e:
            # **シークレットを含まないログにする** (e.payload は GMO のレスポンス本体で
            # 通常 API キーは含まないが、メッセージのみに留めて payload は出さない)
            logger.error(
                "[LIVE ORDER FAILED] %s %s status=%s message=%s",
                d.symbol, d.side, e.status, e.message,
            )
            self._record_live_order(
                d, status=f"live_order_error:{e.status}",
                size_crypto=size_truncated, order_id="", error=str(e),
            )
            # Phase 4c: RiskGuard に reject を通知。連発で HALT する閾値判定は
            # RiskGuard.on_order_reject に委ねる。
            if self._risk_guard is not None:
                self._risk_guard.on_order_reject(e)
            return {
                "status": "live_order_error",
                "symbol": d.symbol,
                "side": d.side,
                "error_status": e.status,
                "error": e.message,
            }

        order_id = api_result.get("order_id", "")
        logger.warning(
            "[LIVE ORDER SENT] %s %s size_crypto=%.8f price_ref=%.2f order_id=%s",
            d.symbol, d.side, size_truncated, d.price_ref, order_id,
        )

        # 約定確認 (Phase 4b)
        fill = self._poll_executions(
            order_id=order_id, ordered_size=size_truncated,
        )

        self._record_live_order(
            d,
            status=fill["status"],
            size_crypto=size_truncated,
            order_id=order_id,
            executed_size=fill["executed_size"],
            fee=fill["fee"],
        )
        return {
            "status": fill["status"],
            "symbol": d.symbol,
            "side": d.side,
            "size_crypto": size_truncated,
            "order_id": order_id,
            "executed_size": fill["executed_size"],
            "fee": fill["fee"],
            "poll_attempts": fill["attempts"],
        }

    def _poll_executions(
        self, order_id: str, ordered_size: float,
    ) -> dict[str, Any]:
        """注文後の約定確認をポーリングし、(filled / partial_fill / not_filled)
        を判定する。

        - 完全約定 (executed >= ordered) → status="filled"、即終了
        - 部分約定 (0 < executed < ordered) → status="partial_fill"
        - 未約定 (executed == 0) → status="not_filled"
        - APIエラーが retry 越しでも回復しなかった場合 → status="executions_unknown"
          (約定したかどうか判定不能。手動確認が必要なので呼び出し側が HALT 判断)
        """
        from gmo_order_client import (  # 遅延 import (テスト容易性)
            extract_executions, sum_executions,
        )
        from gmo_api_client import GmoApiError

        if self._order_client is None or not order_id:
            return {
                "status": "executions_unknown",
                "executed_size": 0.0,
                "fee": 0.0,
                "attempts": 0,
            }

        executed_size = 0.0
        fee = 0.0
        last_error: str | None = None
        attempts = 0
        for attempt in range(1, self._poll_max_attempts + 1):
            attempts = attempt
            try:
                raw = self._order_client.get_executions_by_order_id(order_id)
            except GmoApiError as e:
                last_error = f"executions poll status={e.status} {e.message}"
                logger.warning("executions poll error attempt=%d: %s", attempt, last_error)
            else:
                executions = extract_executions(raw)
                executed_size, fee = sum_executions(executions)
                if ordered_size > 0 and executed_size >= ordered_size:
                    return {
                        "status": "filled",
                        "executed_size": executed_size,
                        "fee": fee,
                        "attempts": attempts,
                    }
            if attempt < self._poll_max_attempts:
                self._sleep_fn(self._poll_interval_sec)

        if last_error is not None and executed_size <= 0:
            # 一度も約定を観測できず最後の試行も API エラーだった → 判定不能
            logger.error(
                "[LIVE FILL UNKNOWN] order_id=%s last=%s", order_id, last_error,
            )
            return {
                "status": "executions_unknown",
                "executed_size": 0.0,
                "fee": 0.0,
                "attempts": attempts,
            }
        if executed_size <= 0:
            return {
                "status": "not_filled",
                "executed_size": 0.0,
                "fee": fee,
                "attempts": attempts,
            }
        return {
            "status": "partial_fill",
            "executed_size": executed_size,
            "fee": fee,
            "attempts": attempts,
        }

    # ------------------------------------------------------------------
    # live 側: 記録 (jsonl + csv)
    # ------------------------------------------------------------------
    def _write_live_csv_header(self) -> None:
        with self.live_csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "ts", "iso_ts", "mode", "symbol", "side",
                "size_jpy", "size_crypto", "price_ref", "order_id",
                "status", "executed_size", "fee", "reason", "error",
            ])

    def _record_live_order(
        self,
        d: Decision,
        *,
        status: str,
        size_crypto: float,
        order_id: str,
        error: str = "",
        executed_size: float = 0.0,
        fee: float = 0.0,
    ) -> None:
        """live 注文の試行を ``data/live_orders.jsonl`` / ``.csv`` に追記する。

        dry-run 用ファイルと分離 (`scripts/aggregate.py` の混乱を避ける)。
        api error メッセージは payload を含めず文字列のみ記録 (シークレット混入予防)。
        """
        now = time.time()
        row: dict[str, Any] = {
            "ts": now,
            "iso_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "mode": "live",
            "symbol": d.symbol,
            "side": d.side,
            "size_jpy": d.size_jpy,
            "size_crypto": size_crypto,
            "price_ref": d.price_ref,
            "order_id": order_id,
            "status": status,
            "executed_size": executed_size,
            "fee": fee,
            "reason": d.reason,
            "error": error,
        }
        try:
            with self.live_jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            with self.live_csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    row["ts"], row["iso_ts"], row["mode"], row["symbol"], row["side"],
                    row["size_jpy"], row["size_crypto"], row["price_ref"],
                    row["order_id"], row["status"], row["executed_size"], row["fee"],
                    row["reason"], row["error"],
                ])
        except OSError as e:
            logger.exception("failed to write live order record: %s", e)

    # ------------------------------------------------------------------
    # dry-run 側
    # ------------------------------------------------------------------
    def _write_csv_header(self) -> None:
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "ts", "iso_ts", "mode", "symbol", "side",
                "size_jpy", "price_ref", "reason", "strong",
            ])

    def _record_dry_run(self, d: Decision) -> dict[str, Any]:
        now = time.time()
        row: dict[str, Any] = {
            "ts": now,
            "iso_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "mode": "dry_run",
            "symbol": d.symbol,
            "side": d.side,
            "size_jpy": d.size_jpy,
            "price_ref": d.price_ref,
            "reason": d.reason,
            "strong": d.strong,
        }

        try:
            with self.jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            with self.csv_path.open("a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    row["ts"], row["iso_ts"], row["mode"], row["symbol"], row["side"],
                    row["size_jpy"], row["price_ref"], row["reason"], row["strong"],
                ])
        except OSError as e:
            logger.exception("failed to write dry-run record: %s", e)
            return {"status": "write_failed", "error": str(e)}

        logger.warning(
            "[DRY-RUN ORDER] %s %s jpy=%.0f price=%.2f reason=%s",
            d.symbol, d.side, d.size_jpy, d.price_ref, d.reason,
        )
        return {"status": "recorded", "row": row}
