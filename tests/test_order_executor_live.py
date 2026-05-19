"""OrderExecutor._send_live_order_impl のテスト。

`_send_live_order` 本体 (NotImplementedError) は触らず、Phase 4 で追加した
実装本体メソッド `_send_live_order_impl` を直接呼んで検証する。
gate3 物理保証 (NotImplementedError) は既存 `tests/test_smoke.py::TestLiveGates`
で別途検査されているため本ファイルでは扱わない。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gmo_api_client import GmoApiError  # noqa: E402
from order_executor import OrderExecutor  # noqa: E402
from risk_guard import Decision  # noqa: E402


# ----------------------------------------------------------------------
# 共通 fixture
# ----------------------------------------------------------------------
BASE_CFG: dict = {
    "order": {
        "min_size_by_symbol": {
            "BTC_JPY": 0.0001,
            "ETH_JPY": 0.01,
        },
        "size_decimals": {
            "BTC_JPY": 4,
            "ETH_JPY": 2,
        },
        # ポーリング・retry はテストで無音動作させる
        "executions_poll": {
            "max_attempts": 3,
            "interval_sec": 0.0,
        },
    },
}


class _FakeOrderClient:
    """GmoOrderClient の差し替え用 fake。

    - `place_market_order`: 引数を記録 + 任意レスポンス
    - `get_executions_by_order_id`: 引数を記録 + 任意レスポンス (sequence で異なる
      応答を返せる)。`executions_responses` を渡さない場合は「直前の place_market_order
      の size と同じ executedSize で完全約定した」レスポンスをデフォルトで返す。
    """

    def __init__(
        self,
        response: dict | None = None,
        raise_exc: Exception | None = None,
        executions_responses: list | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.executions_calls: list[str] = []
        self._response = response or {
            "order_id": "FAKE_999",
            "raw": {"status": 0, "data": "FAKE_999"},
        }
        self._raise_exc = raise_exc
        self._executions_responses = executions_responses
        self._executions_idx = 0

    def place_market_order(
        self,
        symbol: str,
        side: str,
        size_crypto: float,
        size_decimals: int,
    ) -> dict:
        self.calls.append({
            "symbol": symbol,
            "side": side,
            "size_crypto": size_crypto,
            "size_decimals": size_decimals,
        })
        if self._raise_exc:
            raise self._raise_exc
        return self._response

    def get_executions_by_order_id(self, order_id: str) -> dict:
        self.executions_calls.append(order_id)
        if self._executions_responses is not None:
            resp = self._executions_responses[
                min(self._executions_idx, len(self._executions_responses) - 1)
            ]
            self._executions_idx += 1
            if isinstance(resp, Exception):
                raise resp
            return resp
        # デフォルト: 直近の place_market_order の size と同じ executedSize で完全約定
        last_size = self.calls[-1]["size_crypto"] if self.calls else 0.0
        return {
            "status": 0,
            "data": [{
                "orderId": order_id,
                "executedSize": f"{last_size:.8f}",
                "fee": "0",
            }],
        }


def _decision(
    symbol: str = "BTC_JPY",
    side: str = "buy",
    size_jpy: float = 10_000,
    price_ref: float = 10_000_000,
) -> Decision:
    return Decision(
        symbol=symbol, side=side, size_jpy=size_jpy, price_ref=price_ref,
        reason="test", strong=False,
    )


class _LiveOrderTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        os.environ["STATE_DIR"] = self._td.name
        self.live_jsonl = Path(self._td.name) / "live_orders.jsonl"

    def tearDown(self) -> None:
        os.environ.pop("STATE_DIR", None)
        self._td.cleanup()


# ----------------------------------------------------------------------
# 異常系: 設定や入力が崩れているケース
# ----------------------------------------------------------------------
class TestSendLiveOrderImplGuards(_LiveOrderTestBase):
    def test_no_order_client_returns_status(self) -> None:
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=None)
        result = ex._send_live_order_impl(_decision())
        self.assertEqual(result["status"], "no_order_client")
        # API は呼ばれていない → jsonl に書かれない
        self.assertFalse(self.live_jsonl.exists())

    def test_invalid_side(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision(side="hold"))
        self.assertEqual(result["status"], "invalid_side")
        self.assertEqual(len(fake.calls), 0)

    def test_invalid_price(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision(price_ref=0.0))
        self.assertEqual(result["status"], "invalid_price")
        self.assertEqual(len(fake.calls), 0)

    def test_unknown_symbol(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision(symbol="UNKNOWN_JPY"))
        self.assertEqual(result["status"], "unknown_symbol")
        self.assertEqual(len(fake.calls), 0)


# ----------------------------------------------------------------------
# 最小ロット未満は送信せず記録のみ
# ----------------------------------------------------------------------
class TestBelowMinSize(_LiveOrderTestBase):
    def test_below_min_blocks_and_records(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        # 0.00009 BTC (= 900 JPY / 10M JPY/BTC) は min 0.0001 未満
        result = ex._send_live_order_impl(
            _decision(size_jpy=900, price_ref=10_000_000),
        )
        self.assertEqual(result["status"], "below_min_size")
        self.assertEqual(len(fake.calls), 0)
        # 記録は書かれている
        self.assertTrue(self.live_jsonl.exists())
        rec = json.loads(self.live_jsonl.read_text(encoding="utf-8").strip())
        self.assertEqual(rec["status"], "below_min_size")
        self.assertEqual(rec["order_id"], "")

    def test_truncation_can_push_below_min(self) -> None:
        """0.00015 BTC は 4 桁切り捨てで 0.0001 → 最小ぎりぎり通過の境界。"""
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        # 0.00015 BTC = 1500 JPY / 10M
        result = ex._send_live_order_impl(
            _decision(size_jpy=1500, price_ref=10_000_000),
        )
        # 切り捨てで 0.0001 == min → 通過、デフォルト fake は完全約定 → filled
        self.assertEqual(result["status"], "filled")
        self.assertEqual(len(fake.calls), 1)

    def test_truncation_strictly_below_min_blocks(self) -> None:
        """0.00009 BTC は 4 桁切り捨てで 0 → min より厳しく下回り blocked。"""
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(
            _decision(size_jpy=900, price_ref=10_000_000),
        )
        self.assertEqual(result["status"], "below_min_size")


# ----------------------------------------------------------------------
# 正常系
# ----------------------------------------------------------------------
class TestSendLiveOrderImplSuccess(_LiveOrderTestBase):
    def test_calls_order_client_with_correct_args(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        ex._send_live_order_impl(
            _decision(size_jpy=10_000, price_ref=10_000_000),
        )
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["symbol"], "BTC_JPY")
        self.assertEqual(call["side"], "buy")
        # 10000 / 10_000_000 = 0.001
        self.assertAlmostEqual(call["size_crypto"], 0.001, places=8)
        self.assertEqual(call["size_decimals"], 4)

    def test_returns_order_id_on_success(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision())
        # デフォルト fake は約定 size を返すので filled
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["order_id"], "FAKE_999")

    def test_writes_jsonl_record_on_success(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        ex._send_live_order_impl(
            _decision(size_jpy=10_000, price_ref=10_000_000),
        )
        self.assertTrue(self.live_jsonl.exists())
        rec = json.loads(self.live_jsonl.read_text(encoding="utf-8").strip())
        self.assertEqual(rec["status"], "filled")
        self.assertEqual(rec["order_id"], "FAKE_999")
        self.assertEqual(rec["mode"], "live")
        self.assertEqual(rec["symbol"], "BTC_JPY")
        self.assertEqual(rec["side"], "buy")
        # size_crypto は config の size_decimals=4 で切り捨てた値
        self.assertAlmostEqual(rec["size_crypto"], 0.001, places=8)
        # 約定数量と fee も記録される (Phase 4b)
        self.assertAlmostEqual(rec["executed_size"], 0.001, places=8)
        self.assertEqual(rec["fee"], 0.0)


# ----------------------------------------------------------------------
# API エラー時
# ----------------------------------------------------------------------
class TestSendLiveOrderImplApiError(_LiveOrderTestBase):
    def test_gmo_api_error_returns_live_order_error(self) -> None:
        err = GmoApiError(
            status=5,
            message="insufficient balance",
            payload={"status": 5, "messages": [{"message_string": "insufficient balance"}]},
        )
        fake = _FakeOrderClient(raise_exc=err)
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision())
        self.assertEqual(result["status"], "live_order_error")
        self.assertEqual(result["error_status"], 5)
        self.assertIn("insufficient balance", result["error"])

    def test_api_error_records_failure_jsonl(self) -> None:
        err = GmoApiError(
            status=5, message="insufficient balance", payload=None,
        )
        fake = _FakeOrderClient(raise_exc=err)
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        ex._send_live_order_impl(_decision())
        self.assertTrue(self.live_jsonl.exists())
        rec = json.loads(self.live_jsonl.read_text(encoding="utf-8").strip())
        self.assertTrue(rec["status"].startswith("live_order_error"))
        self.assertEqual(rec["order_id"], "")

    def test_api_error_log_does_not_leak_payload(self) -> None:
        """payload (GMO レスポンス丸ごと) を str() に含めない (ログ漏えい予防)。"""
        sensitive_payload = {
            "status": 5,
            "messages": [{"message_string": "SECRET_DEBUG_TRACE_xxxx"}],
            "internal_token_should_not_leak": "abcdef",
        }
        err = GmoApiError(status=5, message="masked", payload=sensitive_payload)
        fake = _FakeOrderClient(raise_exc=err)
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        ex._send_live_order_impl(_decision())
        rec_text = self.live_jsonl.read_text(encoding="utf-8")
        # payload 内部のキー名や値が記録に流れ込んでいないことを確認
        self.assertNotIn("internal_token_should_not_leak", rec_text)
        self.assertNotIn("abcdef", rec_text)


# ----------------------------------------------------------------------
# Phase 4b: executions ポーリングと約定判定
# ----------------------------------------------------------------------
class TestExecutionsPolling(_LiveOrderTestBase):
    def test_partial_fill_detected(self) -> None:
        """executedSize < ordered_size のまま max_attempts に到達 → partial_fill。"""
        fake = _FakeOrderClient(
            executions_responses=[
                # 全 attempt で 0.0005 BTC しか約定していない (orderedSize=0.001)
                {"status": 0, "data": [{
                    "orderId": "FAKE_999",
                    "executedSize": "0.0005",
                    "fee": "1.5",
                }]},
            ],
        )
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(
            _decision(size_jpy=10_000, price_ref=10_000_000),
        )
        self.assertEqual(result["status"], "partial_fill")
        self.assertAlmostEqual(result["executed_size"], 0.0005, places=8)
        # ポーリング 1 回目で既に 0.0005 がある → max_attempts まで回り続ける
        # (完全約定にならないため break しない)
        self.assertEqual(result["poll_attempts"], 3)

    def test_not_filled_when_executions_empty(self) -> None:
        """executions が空のまま max_attempts → not_filled。"""
        fake = _FakeOrderClient(
            executions_responses=[
                {"status": 0, "data": []},
            ],
        )
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision())
        self.assertEqual(result["status"], "not_filled")
        self.assertEqual(result["executed_size"], 0.0)
        self.assertEqual(result["poll_attempts"], 3)

    def test_filled_after_polling_retries(self) -> None:
        """初回 0 → 2 回目で完全約定 → filled、attempts=2 で break。"""
        fake = _FakeOrderClient(
            executions_responses=[
                {"status": 0, "data": []},
                {"status": 0, "data": [{
                    "orderId": "FAKE_999",
                    "executedSize": "0.001",
                    "fee": "2.0",
                }]},
            ],
        )
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision())
        self.assertEqual(result["status"], "filled")
        self.assertAlmostEqual(result["executed_size"], 0.001, places=8)
        self.assertAlmostEqual(result["fee"], 2.0, places=8)
        self.assertEqual(result["poll_attempts"], 2)

    def test_executions_unknown_on_api_error(self) -> None:
        """ポーリング全試行で API エラー → executions_unknown (判定不能、要手動確認)。"""
        from gmo_api_client import GmoApiError
        fake = _FakeOrderClient(
            executions_responses=[
                GmoApiError(status=500, message="server error"),
            ],
        )
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(_decision())
        self.assertEqual(result["status"], "executions_unknown")
        self.assertEqual(result["executed_size"], 0.0)

    def test_fee_is_summed_across_executions(self) -> None:
        """複数 execution の fee は合計される。"""
        fake = _FakeOrderClient(
            executions_responses=[
                {"status": 0, "data": [
                    {"orderId": "FAKE_999", "executedSize": "0.0005", "fee": "1.0"},
                    {"orderId": "FAKE_999", "executedSize": "0.0005", "fee": "1.5"},
                ]},
            ],
        )
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order_impl(
            _decision(size_jpy=10_000, price_ref=10_000_000),
        )
        # 合計 executedSize=0.001 が ordered=0.001 と一致 → filled
        self.assertEqual(result["status"], "filled")
        self.assertAlmostEqual(result["fee"], 2.5, places=8)


# ----------------------------------------------------------------------
# Phase 4c: RiskGuard との連携と HALT 統合
# ----------------------------------------------------------------------
class TestRiskGuardIntegration(_LiveOrderTestBase):
    """live 注文 reject → RiskGuard.on_order_reject → HALT までの結線。

    RiskGuard を fake で渡さず本物を作って統合的に動作確認する。
    """

    def _integrated_cfg(self, max_rejects: int = 3) -> dict:
        return {
            **BASE_CFG,
            "risk": {
                "max_order_rejects_consecutive": max_rejects,
                "max_consecutive_errors": 5,
                "halt_on_error": True,
            },
            "limits": {},
            "scorer": {"thresholds": {}},
            "exits": {},
            "loop": {},
            "symbols": {"core": [], "satellite": []},
        }

    def _make_guard(self, cfg: dict):
        from risk_guard import RiskGuard
        from state_store import StateStore
        state = StateStore(path=str(Path(self._td.name) / "state.json"))
        return RiskGuard(cfg, state), state

    def test_api_error_calls_on_order_reject(self) -> None:
        cfg = self._integrated_cfg(max_rejects=5)
        guard, state = self._make_guard(cfg)
        err = GmoApiError(
            status=5, message="insufficient balance", payload=None,
        )
        fake = _FakeOrderClient(raise_exc=err)
        ex = OrderExecutor(cfg, mode="dry_run", order_client=fake, risk_guard=guard)
        ex._send_live_order_impl(_decision())
        self.assertEqual(state.order_reject_count(), 1)
        self.assertFalse(guard.is_halted())

    def test_three_rejects_trigger_halt(self) -> None:
        cfg = self._integrated_cfg(max_rejects=3)
        guard, state = self._make_guard(cfg)
        err = GmoApiError(status=5, message="bad", payload=None)
        fake = _FakeOrderClient(raise_exc=err)
        ex = OrderExecutor(cfg, mode="dry_run", order_client=fake, risk_guard=guard)
        ex._send_live_order_impl(_decision())
        ex._send_live_order_impl(_decision())
        ex._send_live_order_impl(_decision())
        self.assertEqual(state.order_reject_count(), 3)
        self.assertTrue(guard.is_halted())
        self.assertIn("order_rejects", state.halt_reason())

    def test_success_resets_reject_counter(self) -> None:
        """reject 2 回 → 成功 1 回 (guard.on_success) → reject カウンタ=0。"""
        cfg = self._integrated_cfg(max_rejects=3)
        guard, state = self._make_guard(cfg)

        # 2 回 reject (HALT 一歩手前)
        err = GmoApiError(status=5, message="bad", payload=None)
        fake_fail = _FakeOrderClient(raise_exc=err)
        ex = OrderExecutor(cfg, mode="dry_run", order_client=fake_fail, risk_guard=guard)
        ex._send_live_order_impl(_decision())
        ex._send_live_order_impl(_decision())
        self.assertEqual(state.order_reject_count(), 2)

        # main.py のサイクル終端を模倣
        guard.on_success()
        self.assertEqual(state.order_reject_count(), 0)

        # 次の reject は 1 回目から再カウント (HALT しない)
        ex._send_live_order_impl(_decision())
        self.assertEqual(state.order_reject_count(), 1)
        self.assertFalse(guard.is_halted())

    def test_no_risk_guard_does_not_crash(self) -> None:
        """risk_guard=None でも GmoApiError は普通に返るだけで例外を吐かない。"""
        err = GmoApiError(status=5, message="bad", payload=None)
        fake = _FakeOrderClient(raise_exc=err)
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake, risk_guard=None)
        result = ex._send_live_order_impl(_decision())
        self.assertEqual(result["status"], "live_order_error")


# ----------------------------------------------------------------------
# Phase 5 以降: gate1 と gate3 が両方開いた状態の挙動確認
# (旧クラス名 TestSendLiveOrderRemainsNotImplemented を Phase 5 で改名)
# ----------------------------------------------------------------------
class TestSendLiveOrderRoutesToImpl(_LiveOrderTestBase):
    """Phase 5 で `_send_live_order` は `_send_live_order_impl` への薄い委譲。

    Phase 4 までは「`_send_live_order` が NotImplementedError を raise する」
    という gate3 物理保証を assert していたが、Phase 5 の単独 PR で
    `return self._send_live_order_impl(d)` に置き換わった。本クラスは
    その新しい配線を回帰防止のために assert する。
    """

    def test_send_live_order_routes_to_impl(self) -> None:
        """`_send_live_order` を呼ぶと `_send_live_order_impl` の結果が返る。

        デフォルトの `_FakeOrderClient` は完全約定を返すので filled になる。
        """
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        result = ex._send_live_order(_decision())
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["order_id"], "FAKE_999")

    def test_enable_live_order_constant_is_true(self) -> None:
        """Phase 5 の単独 PR で gate1 が True に切り替わった事実を assert。

        以降このフラグが False に戻る場合は **緊急停止** を意味するため、
        その変更は単独 PR で人間レビューを経て行う運用とする (DESIGN.md §7)。
        """
        import order_executor as oe
        self.assertTrue(oe.ENABLE_LIVE_ORDER)


if __name__ == "__main__":
    unittest.main()
