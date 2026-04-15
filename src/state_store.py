"""状態の永続化。

保有ポジション・クールダウン・HALT フラグ・連続エラー数・スコア時刻を
`data/state.json` に保存する。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    size_jpy: float
    entry_price: float
    entry_ts: float
    highest_px: float = 0.0  # trail 用。0 なら entry_price を使う（後方互換）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        entry_price = float(d["entry_price"])
        return cls(
            symbol=str(d["symbol"]),
            size_jpy=float(d["size_jpy"]),
            entry_price=entry_price,
            entry_ts=float(d["entry_ts"]),
            highest_px=float(d.get("highest_px", 0.0) or entry_price),
        )


class StateStore:
    def __init__(self, path: str | None = None) -> None:
        base = Path(os.environ.get("STATE_DIR", "./data"))
        base.mkdir(parents=True, exist_ok=True)
        self.path: Path = Path(path) if path else base / "state.json"
        self._state: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------
    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "halted": False,
            "halt_reason": "",
            "halted_at": 0.0,
            "error_count": 0,
            "positions": {},       # symbol -> Position.to_dict()
            "cooldown_until": {},  # symbol -> epoch
            "last_score_ts": 0.0,
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            # 欠損キー補完
            merged = self._empty()
            merged.update(data)
            return merged
        except Exception as e:
            logger.exception("failed to load state, using empty: %s", e)
            return self._empty()

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # HALT
    # ------------------------------------------------------------------
    def is_halted(self) -> bool:
        return bool(self._state.get("halted"))

    def halt_reason(self) -> str:
        return str(self._state.get("halt_reason", ""))

    def set_halt(self, reason: str) -> None:
        self._state["halted"] = True
        self._state["halt_reason"] = reason
        self._state["halted_at"] = time.time()
        self.save()

    def clear_halt(self) -> None:
        self._state["halted"] = False
        self._state["halt_reason"] = ""
        self._state["error_count"] = 0
        self.save()

    # ------------------------------------------------------------------
    # エラーカウント
    # ------------------------------------------------------------------
    def error_count(self) -> int:
        return int(self._state.get("error_count", 0))

    def increment_error(self) -> int:
        self._state["error_count"] = self.error_count() + 1
        return int(self._state["error_count"])

    def reset_errors(self) -> None:
        self._state["error_count"] = 0

    # ------------------------------------------------------------------
    # ポジション
    # ------------------------------------------------------------------
    def positions(self) -> dict[str, Position]:
        raw = self._state.get("positions", {}) or {}
        return {sym: Position.from_dict(d) for sym, d in raw.items()}

    def has_position(self, symbol: str) -> bool:
        return symbol in (self._state.get("positions") or {})

    def set_position(self, pos: Position) -> None:
        self._state.setdefault("positions", {})[pos.symbol] = pos.to_dict()

    def remove_position(self, symbol: str) -> None:
        (self._state.get("positions") or {}).pop(symbol, None)

    def update_highest_px(self, symbol: str, price: float) -> None:
        """trail 用: 現在価格が過去最高値を超えたら更新する。"""
        raw = (self._state.get("positions") or {}).get(symbol)
        if not raw:
            return
        cur = float(raw.get("highest_px", 0.0) or raw.get("entry_price", 0.0))
        if price > cur:
            raw["highest_px"] = price

    # ------------------------------------------------------------------
    # クールダウン
    # ------------------------------------------------------------------
    def in_cooldown(self, symbol: str) -> bool:
        until = float((self._state.get("cooldown_until") or {}).get(symbol, 0.0))
        return time.time() < until

    def cooldown_remaining_sec(self, symbol: str) -> float:
        """クールダウンの残秒数。終わっていれば 0。"""
        until = float((self._state.get("cooldown_until") or {}).get(symbol, 0.0))
        return max(0.0, until - time.time())

    def set_cooldown(self, symbol: str, minutes: float) -> None:
        self._state.setdefault("cooldown_until", {})[symbol] = time.time() + minutes * 60.0

    # ------------------------------------------------------------------
    # スコア時刻
    # ------------------------------------------------------------------
    def last_score_ts(self) -> float:
        return float(self._state.get("last_score_ts", 0.0))

    def mark_scored(self) -> None:
        self._state["last_score_ts"] = time.time()
