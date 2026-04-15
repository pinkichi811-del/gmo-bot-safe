"""発注実行。

現段階は **絶対に実注文を出さない**。
dry-run 時は `data/dry_run_orders.jsonl` と `.csv` に記録するのみ。

live 発注は三段ゲートで塞いでいる:
  1. `ENABLE_LIVE_ORDER`（このファイル内の定数・コードゲート）
  2. `LIVE_OK` 環境変数（runtime ゲート・`yes` でのみ通過）
  3. 実装そのものが未着手（`_send_live_order` が NotImplementedError を返す）

live を有効化するには全部のゲートを通す必要がある。単独での有効化は設計上できない。
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from risk_guard import Decision

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
    def __init__(self, cfg: dict[str, Any], mode: str = "dry_run") -> None:
        self.cfg = cfg
        self.mode = mode

        state_dir = Path(os.environ.get("STATE_DIR", "./data"))
        state_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path: Path = state_dir / "dry_run_orders.jsonl"
        self.csv_path: Path = state_dir / "dry_run_orders.csv"

        if not self.csv_path.exists():
            self._write_csv_header()

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
        """実注文送信。**まだ未実装**。

        TODO(live): GMOコイン Private API 呼び出し
          1. nonce + HMAC-SHA256 署名
          2. POST /v1/order (現物、成行 or 指値)
          3. レスポンス検証 / order_id 記録
          4. 約定確認（GET /v1/executions）
          5. 失敗時は RiskGuard.on_error へ伝播
          6. タイムアウト / リトライ戦略
        """
        raise NotImplementedError("live order sending is not implemented")

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
