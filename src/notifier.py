"""通知。

- ConsoleBackend: ログに書き出す（常に有効）
- WebhookBackend: Slack / Discord の Incoming Webhook へ POST（オプション）

どのバックエンドが失敗しても bot を止めない。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from risk_guard import Decision

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Backends
# ----------------------------------------------------------------------
class Backend(ABC):
    @abstractmethod
    def send(self, message: str) -> None: ...


class ConsoleBackend(Backend):
    def send(self, message: str) -> None:
        logger.warning("[NOTIFY] %s", message)


class WebhookBackend(Backend):
    """Slack / Discord 互換 webhook。ペイロードは最小限の JSON。"""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout

    def send(self, message: str) -> None:
        # TODO(live): retry / backoff、ペイロード形式のプロバイダ別切替
        try:
            data = json.dumps({"text": message}).encode("utf-8")
            req = urllib.request.Request(
                self.url,
                data=data,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            logger.warning("webhook send failed: %s", e)


# ----------------------------------------------------------------------
# Notifier
# ----------------------------------------------------------------------
class Notifier:
    def __init__(self, cfg: dict[str, Any]) -> None:
        ncfg = cfg.get("notifier", {}) or {}
        self.enabled: bool = bool(ncfg.get("enabled", False))
        self.on_halt: bool = bool(ncfg.get("on_halt", True))
        self.on_order: bool = bool(ncfg.get("on_order", True))
        self.on_error: bool = bool(ncfg.get("on_error", True))

        self.backends: list[Backend] = [ConsoleBackend()]
        url = (os.environ.get("NOTIFIER_WEBHOOK_URL") or "").strip()
        if self.enabled and url:
            self.backends.append(WebhookBackend(url))

    def _dispatch(self, message: str) -> None:
        for b in self.backends:
            try:
                b.send(message)
            except Exception as e:
                logger.warning("notifier backend failed: %s", e)

    def notify_order(self, d: Decision) -> None:
        if not self.on_order:
            return
        tag = "STRONG" if d.strong else "NORMAL"
        self._dispatch(
            f"[DRY-RUN {tag}] {d.side.upper()} {d.symbol} "
            f"jpy={d.size_jpy:.0f} price={d.price_ref:.2f} ({d.reason})"
        )

    def notify_halt(self, reason: str) -> None:
        if not self.on_halt:
            return
        self._dispatch(f"[HALT] {reason}")

    def notify_error(self, err: Exception) -> None:
        if not self.on_error:
            return
        self._dispatch(f"[ERROR] {type(err).__name__}: {err}")

    def notify_order_reject(self, symbol: str, reason: str) -> None:
        """live 注文 API が reject された時。Phase 4c で導入。

        on_error フラグでフィルタする (注文失敗はエラー扱いの方が観測しやすい)。
        """
        if not self.on_error:
            return
        self._dispatch(f"[ORDER REJECT] {symbol}: {reason}")
