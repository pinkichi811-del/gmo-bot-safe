"""GmoApiClient のテスト。

既存方針 (unittest + Stub) に合わせ、HTTP 層は http_fn の差し替えで mock する。
unittest.mock.patch は使わない。
"""
import hashlib
import hmac
import io
import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gmo_api_client import (  # noqa: E402
    GmoApiClient,
    GmoApiError,
    GmoCredentials,
    sign,
)


# ----------------------------------------------------------------------
# テスト用 http_fn
# ----------------------------------------------------------------------
class _Recorder:
    """http_fn の引数を捕捉し、固定レスポンスを返すスタブ。"""

    def __init__(self, response: bytes | None = None,
                 raise_exc: Exception | None = None) -> None:
        self.requests: list[urllib.request.Request] = []
        self._response = response if response is not None else b'{"status":0,"data":{}}'
        self._raise_exc = raise_exc

    def __call__(self, req: urllib.request.Request, timeout: float) -> bytes:
        self.requests.append(req)
        if self._raise_exc:
            raise self._raise_exc
        return self._response


def _make_http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.com",
        code=code,
        msg=f"status {code}",
        hdrs=None,
        fp=io.BytesIO(body),
    )


# ----------------------------------------------------------------------
# 署名
# ----------------------------------------------------------------------
class TestSign(unittest.TestCase):
    def test_sign_matches_manual_hmac(self) -> None:
        secret = "test_secret"
        ts = "1234567890123"
        method = "GET"
        path = "/v1/account/assets"
        body = ""
        expected = hmac.new(
            secret.encode("utf-8"),
            f"{ts}{method}{path}{body}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(sign(secret, ts, method, path, body), expected)

    def test_sign_changes_with_body(self) -> None:
        s1 = sign("s", "1", "POST", "/p", "")
        s2 = sign("s", "1", "POST", "/p", "x")
        self.assertNotEqual(s1, s2)

    def test_sign_changes_with_timestamp(self) -> None:
        s1 = sign("s", "1", "GET", "/p", "")
        s2 = sign("s", "2", "GET", "/p", "")
        self.assertNotEqual(s1, s2)


# ----------------------------------------------------------------------
# Credentials の repr/str がシークレットを隠す
# ----------------------------------------------------------------------
class TestCredentialsRepr(unittest.TestCase):
    def test_repr_hides_secret(self) -> None:
        c = GmoCredentials(api_key="ABCDEF_KEY", api_secret="ZZZ_SECRET")
        r = repr(c)
        s = str(c)
        self.assertNotIn("ABCDEF_KEY", r)
        self.assertNotIn("ZZZ_SECRET", r)
        self.assertNotIn("ABCDEF_KEY", s)
        self.assertNotIn("ZZZ_SECRET", s)
        self.assertIn("***", r)

    def test_client_repr_hides_credentials(self) -> None:
        c = GmoCredentials(api_key="ABCDEF_KEY", api_secret="ZZZ_SECRET")
        client = GmoApiClient(credentials=c)
        r = repr(client)
        self.assertNotIn("ABCDEF_KEY", r)
        self.assertNotIn("ZZZ_SECRET", r)


# ----------------------------------------------------------------------
# from_env
# ----------------------------------------------------------------------
class TestFromEnv(unittest.TestCase):
    def test_loads_both_keys(self) -> None:
        env = {"GMO_API_KEY": "K", "GMO_API_SECRET": "S"}
        client = GmoApiClient.from_env(env=env)
        # credentials は内部にあるが、署名が成立することで間接的に確認
        self.assertIsNotNone(client._credentials)
        self.assertEqual(client._credentials.api_key, "K")
        self.assertEqual(client._credentials.api_secret, "S")

    def test_missing_key_raises(self) -> None:
        env = {"GMO_API_SECRET": "S"}
        with self.assertRaises(ValueError) as ctx:
            GmoApiClient.from_env(env=env)
        self.assertIn("GMO_API_KEY", str(ctx.exception))

    def test_missing_secret_raises(self) -> None:
        env = {"GMO_API_KEY": "K"}
        with self.assertRaises(ValueError) as ctx:
            GmoApiClient.from_env(env=env)
        self.assertIn("GMO_API_SECRET", str(ctx.exception))

    def test_empty_string_treated_as_missing(self) -> None:
        env = {"GMO_API_KEY": "", "GMO_API_SECRET": ""}
        with self.assertRaises(ValueError) as ctx:
            GmoApiClient.from_env(env=env)
        msg = str(ctx.exception)
        self.assertIn("GMO_API_KEY", msg)
        self.assertIn("GMO_API_SECRET", msg)


# ----------------------------------------------------------------------
# timestamp / nonce
# ----------------------------------------------------------------------
class TestTimestamp(unittest.TestCase):
    def test_timestamp_strictly_increases_under_frozen_clock(self) -> None:
        client = GmoApiClient(clock_fn=lambda: 1700000000.0)
        ts1 = int(client._timestamp_ms())
        ts2 = int(client._timestamp_ms())
        ts3 = int(client._timestamp_ms())
        self.assertLess(ts1, ts2)
        self.assertLess(ts2, ts3)

    def test_timestamp_handles_clock_going_backwards(self) -> None:
        clock_values = iter([1700000000.0, 1699999999.0])
        client = GmoApiClient(clock_fn=lambda: next(clock_values))
        ts1 = int(client._timestamp_ms())
        ts2 = int(client._timestamp_ms())
        self.assertLess(ts1, ts2)


# ----------------------------------------------------------------------
# public エンドポイント
# ----------------------------------------------------------------------
class TestPublicEndpoints(unittest.TestCase):
    def test_get_ticker_builds_url_with_symbol(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":[{"symbol":"BTC","last":"100"}]}')
        client = GmoApiClient(http_fn=rec)
        client.get_ticker("BTC")
        self.assertEqual(len(rec.requests), 1)
        url = rec.requests[0].full_url
        self.assertIn("/v1/ticker", url)
        self.assertIn("symbol=BTC", url)

    def test_get_ticker_returns_parsed_dict(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":[{"symbol":"BTC","last":"100"}]}')
        client = GmoApiClient(http_fn=rec)
        result = client.get_ticker("BTC")
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["data"][0]["symbol"], "BTC")

    def test_get_ticker_does_not_sign(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":[]}')
        client = GmoApiClient(http_fn=rec)
        client.get_ticker("BTC")
        headers = dict(rec.requests[0].header_items())
        # urllib は header 名を Title-Case 化するが、API-KEY のような独自ヘッダは
        # 慣例に従い titlecase 後も大文字。signed=False では存在しない。
        self.assertNotIn("Api-key", headers)
        self.assertNotIn("Api-sign", headers)

    def test_get_klines_builds_url(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":[]}')
        client = GmoApiClient(http_fn=rec)
        client.get_klines("BTC", interval="5min", date="20260518")
        url = rec.requests[0].full_url
        self.assertIn("/v1/klines", url)
        self.assertIn("symbol=BTC", url)
        self.assertIn("interval=5min", url)
        self.assertIn("date=20260518", url)


# ----------------------------------------------------------------------
# private (signed) エンドポイント
# ----------------------------------------------------------------------
class TestSignedEndpoint(unittest.TestCase):
    def test_get_account_assets_adds_signing_headers(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":[]}')
        creds = GmoCredentials(api_key="MYKEY", api_secret="MYSECRET")
        client = GmoApiClient(
            credentials=creds, http_fn=rec, clock_fn=lambda: 1700000000.0
        )
        client.get_account_assets()
        headers = dict(rec.requests[0].header_items())
        # urllib 側で Title-Case 化される
        self.assertIn("Api-key", headers)
        self.assertIn("Api-timestamp", headers)
        self.assertIn("Api-sign", headers)
        self.assertEqual(headers["Api-key"], "MYKEY")

    def test_get_account_assets_signature_matches_sign_function(self) -> None:
        rec = _Recorder(response=b'{"status":0,"data":[]}')
        creds = GmoCredentials(api_key="MYKEY", api_secret="MYSECRET")
        client = GmoApiClient(
            credentials=creds, http_fn=rec, clock_fn=lambda: 1700000000.0
        )
        client.get_account_assets()
        headers = dict(rec.requests[0].header_items())
        ts = headers["Api-timestamp"]
        expected_sig = sign("MYSECRET", ts, "GET", "/v1/account/assets", "")
        self.assertEqual(headers["Api-sign"], expected_sig)

    def test_signed_request_without_credentials_raises(self) -> None:
        rec = _Recorder()
        client = GmoApiClient(http_fn=rec)
        with self.assertRaises(ValueError):
            client.get_account_assets()


# ----------------------------------------------------------------------
# エラー系
# ----------------------------------------------------------------------
class TestErrors(unittest.TestCase):
    def test_http_401_raises_gmo_api_error(self) -> None:
        rec = _Recorder(raise_exc=_make_http_error(401, b"unauthorized"))
        client = GmoApiClient(http_fn=rec)
        with self.assertRaises(GmoApiError) as ctx:
            client.get_ticker("BTC")
        self.assertEqual(ctx.exception.status, 401)

    def test_http_500_raises_gmo_api_error(self) -> None:
        rec = _Recorder(raise_exc=_make_http_error(500, b"server error"))
        client = GmoApiClient(http_fn=rec)
        with self.assertRaises(GmoApiError) as ctx:
            client.get_ticker("BTC")
        self.assertEqual(ctx.exception.status, 500)

    def test_invalid_json_raises_gmo_api_error(self) -> None:
        rec = _Recorder(response=b"<html>not json</html>")
        client = GmoApiClient(http_fn=rec)
        with self.assertRaises(GmoApiError):
            client.get_ticker("BTC")

    def test_gmo_error_response_raises(self) -> None:
        # GMO の status != 0 (例: 認証エラー)
        rec = _Recorder(
            response=b'{"status":1,"messages":[{"message_string":"bad sign"}]}'
        )
        client = GmoApiClient(http_fn=rec)
        with self.assertRaises(GmoApiError) as ctx:
            client.get_ticker("BTC")
        self.assertEqual(ctx.exception.status, 1)
        self.assertIn("bad sign", str(ctx.exception))


# ----------------------------------------------------------------------
# Hard Rule の物理的保証
# ----------------------------------------------------------------------
class TestHardRule(unittest.TestCase):
    def test_post_not_supported(self) -> None:
        """`_request` が GET 以外で呼ばれたら NotImplementedError を返す。

        注文系メソッド (place_order, cancel_order) を作らない方針の最終確認。
        """
        rec = _Recorder()
        client = GmoApiClient(http_fn=rec)
        with self.assertRaises(NotImplementedError):
            client._request("POST", "https://x", "/v1/order")

    def test_no_order_methods_exposed(self) -> None:
        """クライアントの公開 API に注文系メソッドが**存在しない**ことを assert。"""
        forbidden = ["place_order", "cancel_order", "post_order", "submit_order"]
        for name in forbidden:
            self.assertFalse(
                hasattr(GmoApiClient, name),
                f"{name} must not exist on GmoApiClient (Hard Rule)",
            )


if __name__ == "__main__":
    unittest.main()
