"""GMOコイン API クライアント (read-only)。

Phase 3 のスコープは read-only のみ:
- public:  /v1/ticker, /v1/klines
- private: /v1/account/assets （残高取得のみ）

注文系 (POST /v1/order 等) は **物理的にこのクライアントに存在しない**。
Hard Rule (CLAUDE.md #1) を満たすため、`_request` は GET 専用に書いてあり、
`place_order` / `cancel_order` の入口を作らない。

設計判断:
- HTTP 呼び出しは `http_fn` で差し替え可能 (urllib をテストでモックする手段)。
  既存テスト方針 (`tests/` 全体が unittest + Stub、`mock.patch` 不使用) に合わせる。
- `clock_fn` 注入で nonce 単調増加・衝突回避をユニットテストできる。
- 署名 `sign(secret, ts, method, path, body)` はモジュールトップレベル純関数。
  既知入力 → 既知出力の golden test を当てる。
- credentials の `__repr__` / `__str__` はシークレットを伏字化（事故防止層）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


PUBLIC_BASE_URL = "https://api.coin.z.com/public"
PRIVATE_BASE_URL = "https://api.coin.z.com/private"
DEFAULT_TIMEOUT = 30.0


HttpFn = Callable[[urllib.request.Request, float], bytes]


def _default_http(req: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def sign(secret: str, ts_ms: str, method: str, path: str, body: str) -> str:
    """GMOコイン private API の署名を生成する。

    仕様: HMAC-SHA256(secret, timestamp + method + path + body) の hex digest。
    body は GET なら空文字列、POST なら JSON 文字列。

    純関数。テストの golden に当てやすいよう副作用を一切持たない。
    """
    message = f"{ts_ms}{method}{path}{body}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class GmoCredentials:
    api_key: str
    api_secret: str

    def __repr__(self) -> str:
        return "GmoCredentials(api_key=***, api_secret=***)"

    def __str__(self) -> str:
        return self.__repr__()


class GmoApiError(RuntimeError):
    """GMOコイン API 呼び出しの失敗を表す。

    HTTPError / JSON パース失敗 / GMO 側エラーレスポンスを単一の例外に集約する。
    呼び出し側は status / payload を見て判断する。
    """

    def __init__(self, status: int, message: str, payload: Any | None = None) -> None:
        super().__init__(f"GMO API error status={status}: {message}")
        self.status = status
        self.message = message
        self.payload = payload


class GmoApiClient:
    def __init__(
        self,
        credentials: GmoCredentials | None = None,
        *,
        public_base: str = PUBLIC_BASE_URL,
        private_base: str = PRIVATE_BASE_URL,
        http_fn: HttpFn = _default_http,
        clock_fn: Callable[[], float] = time.time,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._credentials = credentials
        self._public_base = public_base.rstrip("/")
        self._private_base = private_base.rstrip("/")
        self._http_fn = http_fn
        self._clock_fn = clock_fn
        self._timeout = timeout
        self._last_ts_ms: int = 0

    def __repr__(self) -> str:
        has_creds = self._credentials is not None
        return f"GmoApiClient(public_base={self._public_base!r}, has_credentials={has_creds})"

    # ------------------------------------------------------------------
    # ファクトリ
    # ------------------------------------------------------------------
    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> "GmoApiClient":
        """環境変数から credentials を読んで client を構築する。

        欠損 / 空文字列は `ValueError` で raise（キー名をメッセージに含める）。
        """
        import os

        e = env if env is not None else os.environ
        api_key = e.get("GMO_API_KEY", "")
        api_secret = e.get("GMO_API_SECRET", "")

        missing = []
        if not api_key:
            missing.append("GMO_API_KEY")
        if not api_secret:
            missing.append("GMO_API_SECRET")
        if missing:
            raise ValueError(
                f"missing required env vars: {', '.join(missing)}"
            )

        creds = GmoCredentials(api_key=api_key, api_secret=api_secret)
        return cls(credentials=creds, **kwargs)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _timestamp_ms(self) -> str:
        """ms 単位のタイムスタンプ。前回値より厳密に大きいことを保証する。

        clock_fn が同値を返したり過去に戻ったりしても nonce 衝突しないよう、
        `last_ts + 1` を最小値とする。
        """
        candidate = int(self._clock_fn() * 1000)
        if candidate <= self._last_ts_ms:
            candidate = self._last_ts_ms + 1
        self._last_ts_ms = candidate
        return str(candidate)

    def _build_url(self, base: str, path: str, params: dict[str, Any] | None) -> str:
        url = f"{base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return url

    def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
    ) -> dict[str, Any]:
        """HTTP GET 専用。POST は意図的に未対応 (Hard Rule)。"""
        if method != "GET":
            raise NotImplementedError(
                "GmoApiClient supports GET only (read-only). "
                "Order endpoints are intentionally out of scope."
            )

        url = self._build_url(base, path, params)
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/json")

        if signed:
            if self._credentials is None:
                raise ValueError("signed request requires credentials")
            ts = self._timestamp_ms()
            signature = sign(
                self._credentials.api_secret, ts, method, path, body=""
            )
            req.add_header("API-KEY", self._credentials.api_key)
            req.add_header("API-TIMESTAMP", ts)
            req.add_header("API-SIGN", signature)

        logger.debug("gmo_api.request method=%s path=%s signed=%s", method, path, signed)

        try:
            raw = self._http_fn(req, self._timeout)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise GmoApiError(status=e.code, message=str(e.reason), payload=body) from e
        except urllib.error.URLError as e:
            raise GmoApiError(status=0, message=str(e.reason)) from e

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise GmoApiError(status=200, message=f"invalid JSON: {e}") from e

        # GMO は status=0 が成功。それ以外は messages[] にエラー詳細。
        if isinstance(payload, dict) and payload.get("status") not in (0, None):
            messages = payload.get("messages") or []
            msg = messages[0].get("message_string") if messages else "unknown error"
            raise GmoApiError(
                status=payload.get("status", -1),
                message=msg,
                payload=payload,
            )

        return payload

    # ------------------------------------------------------------------
    # public エンドポイント
    # ------------------------------------------------------------------
    def get_ticker(self, symbol: str) -> dict[str, Any]:
        """GET /public/v1/ticker?symbol=XXX。signed 不要。"""
        return self._request(
            "GET", self._public_base, "/v1/ticker",
            params={"symbol": symbol}, signed=False,
        )

    def get_klines(self, symbol: str, interval: str, date: str) -> dict[str, Any]:
        """GET /public/v1/klines?symbol=XXX&interval=YYY&date=YYYYMMDD。"""
        return self._request(
            "GET", self._public_base, "/v1/klines",
            params={"symbol": symbol, "interval": interval, "date": date},
            signed=False,
        )

    # ------------------------------------------------------------------
    # private エンドポイント (read-only)
    # ------------------------------------------------------------------
    def get_account_assets(self) -> dict[str, Any]:
        """GET /private/v1/account/assets。残高一覧。credentials 必須。"""
        return self._request(
            "GET", self._private_base, "/v1/account/assets",
            params=None, signed=True,
        )
