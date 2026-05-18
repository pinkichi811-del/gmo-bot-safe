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
    },
}


class _FakeOrderClient:
    """GmoOrderClient の差し替え用 fake。`place_market_order` を記録 + 任意レスポンス。"""

    def __init__(
        self,
        response: dict | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._response = response or {
            "order_id": "FAKE_999",
            "raw": {"status": 0, "data": "FAKE_999"},
        }
        self._raise_exc = raise_exc

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
        # 切り捨てで 0.0001 == min → 通過
        self.assertEqual(result["status"], "sent")
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
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["order_id"], "FAKE_999")

    def test_writes_jsonl_record_on_success(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        ex._send_live_order_impl(
            _decision(size_jpy=10_000, price_ref=10_000_000),
        )
        self.assertTrue(self.live_jsonl.exists())
        rec = json.loads(self.live_jsonl.read_text(encoding="utf-8").strip())
        self.assertEqual(rec["status"], "sent")
        self.assertEqual(rec["order_id"], "FAKE_999")
        self.assertEqual(rec["mode"], "live")
        self.assertEqual(rec["symbol"], "BTC_JPY")
        self.assertEqual(rec["side"], "buy")
        # size_crypto は config の size_decimals=4 で切り捨てた値
        self.assertAlmostEqual(rec["size_crypto"], 0.001, places=8)


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
# Hard Rule 物理保証: `_send_live_order` 自体は依然 NotImplementedError
# ----------------------------------------------------------------------
class TestSendLiveOrderRemainsNotImplemented(_LiveOrderTestBase):
    """Phase 4 の追加コードが gate3 を破壊していないことを再確認する。"""

    def test_send_live_order_still_raises(self) -> None:
        fake = _FakeOrderClient()
        ex = OrderExecutor(BASE_CFG, mode="dry_run", order_client=fake)
        with self.assertRaises(NotImplementedError):
            ex._send_live_order(_decision())

    def test_enable_live_order_constant_is_false(self) -> None:
        import order_executor as oe
        self.assertFalse(oe.ENABLE_LIVE_ORDER)


if __name__ == "__main__":
    unittest.main()
