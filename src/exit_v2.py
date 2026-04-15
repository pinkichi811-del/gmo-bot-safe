"""Exit v2 — EV ベースの利確と、常時有効な安全装置。

ルール:
  (safety rails)  hard stop / trailing stop / max hold が先に発火したら即 exit。
  (EV check)     上記に該当しない場合、p_continue から EV を計算し、
                 EV_hold <= 0 または p_continue < min なら exit。
                 それ以外は hold。

最終判断は必ずルールエンジン側（run_cycle）が行う。このモジュールは「今の bar で
売るべきか」を返すだけで、発注の権限は持たない。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class PContinueModel(Protocol):
    def predict(self, features: dict[str, float]) -> float: ...


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str
    p_continue: float = 0.5
    ev_hold: float = 0.0
    details: dict[str, Any] = None  # type: ignore[assignment]


class ExitV2:
    """候補パラメータ一覧（config.exit_v2 配下）:
      hard_stop_pct:       建値からこの % 割れで即 exit
      trailing_stop_pct:   ピークからこの % 押しで exit
      max_hold_bars:       この本数を超えたら exit
      upside_atr:          EV 計算の upside 目標 (×ATR)
      downside_atr:        EV 計算の downside 目標 (×ATR)
      min_p_continue:      p_continue がこれ未満なら hold 却下
      min_ev_atr:          EV_hold がこの値（×ATR）未満なら hold 却下
    """

    def __init__(
        self, cfg: dict[str, Any], p_continue: PContinueModel,
    ) -> None:
        ev = (cfg.get("exit_v2") or {})
        self.hard_stop_pct = float(ev.get("hard_stop_pct", 2.5))
        self.trailing_stop_pct = float(ev.get("trailing_stop_pct", 1.5))
        self.max_hold_bars = int(ev.get("max_hold_bars", 48))
        self.upside_atr = float(ev.get("upside_atr", 1.0))
        self.downside_atr = float(ev.get("downside_atr", 1.0))
        self.min_p_continue = float(ev.get("min_p_continue", 0.35))
        self.min_ev_atr = float(ev.get("min_ev_atr", -0.3))
        # EV 判定を許す最小経過 bar（それ未満は safety rails のみ）
        self.min_hold_bars_for_ev = int(ev.get("min_hold_bars_for_ev", 4))
        # トレーリングを有効化する最小含み益（ATR 単位）
        self.trailing_activate_atr = float(ev.get("trailing_activate_atr", 0.5))
        self.p_continue = p_continue

    def evaluate(
        self,
        entry_price: float,
        entry_atr: float,
        current_price: float,
        peak_price: float,
        bars_held: int,
        features: dict[str, float],
    ) -> ExitDecision:
        # ---- safety rails (常時有効) ----
        # hard stop
        if entry_price > 0:
            drop_pct = (current_price - entry_price) / entry_price * 100.0
            if drop_pct <= -self.hard_stop_pct:
                return ExitDecision(
                    True, f"hard_stop {drop_pct:+.2f}%",
                    details={"drop_pct": drop_pct},
                )

        # trailing stop
        #   - 含み益が trailing_activate_atr × ATR を超えてから有効化
        if (peak_price > entry_price and peak_price > 0
                and entry_atr > 0):
            peak_atr = (peak_price - entry_price) / entry_atr
            if peak_atr >= self.trailing_activate_atr:
                retrace_pct = (current_price - peak_price) / peak_price * 100.0
                if retrace_pct <= -self.trailing_stop_pct:
                    return ExitDecision(
                        True, f"trailing_stop {retrace_pct:+.2f}%",
                        details={"retrace_pct": retrace_pct, "peak": peak_price},
                    )

        # max hold
        if bars_held >= self.max_hold_bars:
            return ExitDecision(
                True, f"max_hold {bars_held}",
                details={"bars_held": bars_held},
            )

        # ---- EV-based decision (grace period 終了後のみ) ----
        if bars_held < self.min_hold_bars_for_ev:
            return ExitDecision(
                False, reason="hold_grace",
                details={"bars_held": bars_held,
                         "grace": self.min_hold_bars_for_ev},
            )

        p = self.p_continue.predict(features)
        ev_hold = p * self.upside_atr - (1.0 - p) * self.downside_atr  # ATR 単位

        # p_continue による exit は「明確に負けと判断した時のみ」
        if p < self.min_p_continue and ev_hold < self.min_ev_atr:
            return ExitDecision(
                True, f"low_ev p={p:.2f} ev={ev_hold:+.2f}",
                p_continue=p, ev_hold=ev_hold,
                details={"reason_code": "p_and_ev_both_weak"},
            )

        return ExitDecision(
            False, reason="hold",
            p_continue=p, ev_hold=ev_hold,
            details={"p": p, "ev": ev_hold},
        )
