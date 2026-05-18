"""GmoOrderClient (POST 系) のテスト。

既存方針 (unittest + _Recorder スタブ) に合わせる。
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gmo_api_client import GmoApiError, GmoCredentials, sign  # noqa: E402
from gmo_order_client import (  # noqa: E402
    GmoOrderClient,
    _extract_order_id,
    format_size,
)


# ----------------------------------------------------------------------
# 共通スタブ
# ----------------------------------------------------------------------
class _Recorder:
    def __init__(
        self,
        response: bytes | None = None,
        raise_exc: Exception | None = None,
        responses: list[bytes] | None = None,
    ) -> None:
        self.requests: list[urllib.request.Request] = []
        self._response = response if response is not None else b'{"status":0,"data":"O12345"}'
        self._raise_exc = raise_exc
        self._responses = responses
        self._idx = 0

    def __call__(self, req: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append(req)
        if self._raise_exc:
            raise self._raise_exc
        if self._responses is not None:
            r = self._responses[self._idx]
            self._idx += 1
            return r
        return self._response


def _make_http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.com",
        code=code,
        msg=f"status {code}",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def _creds() -> GmoCredentials:
    return GmoCredentials(api_key="MYKEY", api_secret="MYSECRET")


# ----------------------------------------------------------------------
# format_size
# ----------------------------------------------------------------------
class TestFormatSize(unittest.TestCase):
    def test_btc_truncates_to_four_decimals(self) -> None:
        # 0.00015 BTC → 4桁切り捨てで 0.0001
        self.assertEqual(format_size(0.00015, 4), "0.0001")

    def test_keeps_fixed_decimals(self) -> None:
        # 0.5 BTC を 4 桁表記で
        self.assertEqual(format_size(0.5, 4), "0.5000")

    def test_zero_decimals_returns_int_string(self) -> None:
        # XRP は整数。1.9 → 1
        self.assertEqual(format_size(1.9, 0), "1")

    def test_truncate_does_not_round_up(self) -> None:
        # 0.99999 BTC, 4桁 → 0.9999 (切り上げでは無く切り捨て)
        self.assertEqual(format_size(0.99999, 4), "0.9999")

    def test_negative_decimals_raises(self) -> None:
        with self.assertRaises(ValueError):
            format_size(1.0, -1)


# ----------------------------------------------------------------------
# _extract_order_id
# ----------------------------------------------------------------------
class TestExtractOrderId(unittest.TestCase):
    def test_data_as_string(self) -> None:
        self.assertEqual(_extract_order_id({"status": 0, "data": "O999"}), "O999")

    def test_data_as_dict_with_order_id(self) -> None:
        self.assertEqual(
            _extract_order_id({"status": 0, "data": {"orderId": "X42"}}),
            "X42",
        )

    def test_data_as_int(self) -> None:
        self.assertEqual(_extract_order_id({"status": 0, "data": 12345}), "12345")

    def test_missing_data_raises(self) -> None:
        with self.assertRaises(GmoApiError):
            _extract_order_id({"status": 0})


# ----------------------------------------------------------------------
# place_market_order
# ----------------------------------------------------------------------
class TestPlaceMarketOrder(unittest.TestCase):
    def test_post_method_and_url(self) -> None:
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.place_market_order("BTC_JPY", "BUY", 0.001, 4)

        self.assertEqual(len(rec.requests), 1)
        req = rec.requests[0]
        self.assertEqual(req.method, "POST")
        self.assertIn("/v1/order", req.full_url)

    def test_body_contains_expected_fields(self) -> None:
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.place_market_order("BTC_JPY", "BUY", 0.001, 4)

        body = json.loads(rec.requests[0].data.decode("utf-8"))
        self.assertEqual(body["symbol"], "BTC_JPY")
        self.assertEqual(body["side"], "BUY")
        self.assertEqual(body["executionType"], "MARKET")
        # size は文字列でなければならない (GMO 仕様)
        self.assertIsInstance(body["size"], str)
        self.assertEqual(body["size"], "0.0010")

    def test_side_lowercase_input_is_uppercased(self) -> None:
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.place_market_order("BTC_JPY", "buy", 0.001, 4)
        body = json.loads(rec.requests[0].data.decode("utf-8"))
        self.assertEqual(body["side"], "BUY")

    def test_invalid_side_raises(self) -> None:
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec)
        with self.assertRaises(ValueError):
            client.place_market_order("BTC_JPY", "hold", 0.001, 4)

    def test_zero_size_raises(self) -> None:
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec)
        with self.assertRaises(ValueError):
            client.place_market_order("BTC_JPY", "BUY", 0.0, 4)

    def test_signing_headers_present(self) -> None:
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.place_market_order("BTC_JPY", "BUY", 0.001, 4)
        headers = dict(rec.requests[0].header_items())
        # urllib は header 名を Title-Case 化する
        self.assertIn("Api-key", headers)
        self.assertIn("Api-timestamp", headers)
        self.assertIn("Api-sign", headers)
        self.assertEqual(headers["Api-key"], "MYKEY")
        self.assertEqual(headers["Content-type"], "application/json")

    def test_signature_matches_sign_function(self) -> None:
        """body 込みの sign が `sign(secret, ts, "POST", path, body)` と一致する。"""
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.place_market_order("BTC_JPY", "BUY", 0.001, 4)

        req = rec.requests[0]
        body = req.data.decode("utf-8")
        headers = dict(req.header_items())
        ts = headers["Api-timestamp"]
        expected = sign("MYSECRET", ts, "POST", "/v1/order", body)
        self.assertEqual(headers["Api-sign"], expected)

    def test_signature_is_manual_hmac_of_ts_method_path_body(self) -> None:
        """sign 関数を経由せず手計算した値とも一致 (golden test)。"""
        rec = _Recorder()
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.place_market_order("BTC_JPY", "BUY", 0.001, 4)
        req = rec.requests[0]
        body = req.data.decode("utf-8")
        headers = dict(req.header_items())
        ts = headers["Api-timestamp"]
        expected = hmac.new(
            b"MYSECRET",
            f"{ts}POST/v1/order{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(headers["Api-sign"], expected)

    def test_response_returns_order_id(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":"ORDER_999"}')
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        result = client.place_market_order("BTC_JPY", "BUY", 0.001, 4)
        self.assertEqual(result["order_id"], "ORDER_999")
        self.assertEqual(result["raw"]["status"], 0)


# ----------------------------------------------------------------------
# get_executions_by_order_id
# ----------------------------------------------------------------------
class TestGetExecutions(unittest.TestCase):
    def test_get_method_url_and_query(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":{"list":[]}}')
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.get_executions_by_order_id("ORDER_999")
        req = rec.requests[0]
        self.assertEqual(req.method, "GET")
        self.assertIn("/v1/executions", req.full_url)
        self.assertIn("orderId=ORDER_999", req.full_url)

    def test_signing_for_executions(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":{"list":[]}}')
        client = GmoOrderClient(_creds(), http_fn=rec, clock_fn=lambda: 1700000000.0)
        client.get_executions_by_order_id("ORDER_999")
        headers = dict(rec.requests[0].header_items())
        ts = headers["Api-timestamp"]
        # 署名対象 path に query を含めない (GMO 仕様)
        expected = sign("MYSECRET", ts, "GET", "/v1/executions", "")
        self.assertEqual(headers["Api-sign"], expected)


# ----------------------------------------------------------------------
# エラー系
# ----------------------------------------------------------------------
class TestErrors(unittest.TestCase):
    def test_http_500_raises_gmo_api_error(self) -> None:
        rec = _Recorder(raise_exc=_make_http_error(500, b"server error"))
        client = GmoOrderClient(_creds(), http_fn=rec)
        with self.assertRaises(GmoApiError) as ctx:
            client.place_market_order("BTC_JPY", "BUY", 0.001, 4)
        self.assertEqual(ctx.exception.status, 500)

    def test_http_401_raises_gmo_api_error(self) -> None:
        rec = _Recorder(raise_exc=_make_http_error(401, b"unauthorized"))
        client = GmoOrderClient(_creds(), http_fn=rec)
        with self.assertRaises(GmoApiError) as ctx:
            client.place_market_order("BTC_JPY", "BUY", 0.001, 4)
        self.assertEqual(ctx.exception.status, 401)

    def test_gmo_status_error_raises(self) -> None:
        rec = _Recorder(
            response=b'{"status":5,"messages":[{"message_string":"insufficient funds"}]}'
        )
        client = GmoOrderClient(_creds(), http_fn=rec)
        with self.assertRaises(GmoApiError) as ctx:
            client.place_market_order("BTC_JPY", "BUY", 0.001, 4)
        self.assertEqual(ctx.exception.status, 5)
        self.assertIn("insufficient funds", str(ctx.exception))

    def test_invalid_json_raises_gmo_api_error(self) -> None:
        rec = _Recorder(response=b"<html>not json</html>")
        client = GmoOrderClient(_creds(), http_fn=rec)
        with self.assertRaises(GmoApiError):
            client.place_market_order("BTC_JPY", "BUY", 0.001, 4)

    def test_url_error_raises_gmo_api_error(self) -> None:
        rec = _Recorder(raise_exc=urllib.error.URLError("conn refused"))
        client = GmoOrderClient(_creds(), http_fn=rec)
        with self.assertRaises(GmoApiError) as ctx:
            client.place_market_order("BTC_JPY", "BUY", 0.001, 4)
        self.assertEqual(ctx.exception.status, 0)


# ----------------------------------------------------------------------
# 秘密情報マスク
# ----------------------------------------------------------------------
class TestSecretMasking(unittest.TestCase):
    def test_repr_does_not_leak_secret(self) -> None:
        client = GmoOrderClient(_creds())
        r = repr(client)
        self.assertNotIn("MYKEY", r)
        self.assertNotIn("MYSECRET", r)

    def test_credentials_repr_hides_secret(self) -> None:
        c = _creds()
        self.assertNotIn("MYKEY", repr(c))
        self.assertNotIn("MYSECRET", repr(c))


# ----------------------------------------------------------------------
# nonce / timestamp
# ----------------------------------------------------------------------
class TestNonceMonotonic(unittest.TestCase):
    def test_timestamp_strictly_increases_under_frozen_clock(self) -> None:
        client = GmoOrderClient(_creds(), clock_fn=lambda: 1700000000.0)
        ts1 = int(client._timestamp_ms())
        ts2 = int(client._timestamp_ms())
        ts3 = int(client._timestamp_ms())
        self.assertLess(ts1, ts2)
        self.assertLess(ts2, ts3)


# ----------------------------------------------------------------------
# 構築時の制約
# ----------------------------------------------------------------------
class TestConstructor(unittest.TestCase):
    def test_none_credentials_raises(self) -> None:
        with self.assertRaises(ValueError):
            GmoOrderClient(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
