"""GMOコイン Private API クライアント (POST 系・注文発火専用)。

Phase 4 のスコープ。`gmo_api_client.GmoApiClient` (GET-only) とは **別クラス** で
ある点に強い意味がある:

- `gmo_api_client.py` は CLAUDE.md Hard Rule の物理ガードとして GET 専用で固定。
  POST 経路をあのファイルに足すと `test_post_not_supported` / `test_no_order_methods_exposed`
  が崩れ、Hard Rule の自動検査が無効化される。
- 一方 live 注文の実装はどこかに置く必要があるため、責務を分けて本ファイルに置く。
- 本クラスを呼ぶ口は `order_executor.py` の `_send_live_order_impl` のみ。
  `_send_live_order` 自体は依然 `NotImplementedError` を返す (gate3 維持)。

依存注入パターンは `GmoApiClient` と同じ:
- `http_fn` 差し替えで urllib をモック (既存テスト方針: unittest + Stub)
- `clock_fn` 差し替えで nonce 単調増加をユニットテスト可能
"""
from __future__ import annotations

import json
import logging
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from gmo_api_client import (
    DEFAULT_TIMEOUT,
    PRIVATE_BASE_URL,
    GmoApiError,
    GmoCredentials,
    HttpFn,
    _default_http,
    sign,
)

logger = logging.getLogger(__name__)

# Retry が許される HTTP ステータス。429 = レート制限、5xx = サーバ側不調。
# 4xx (auth / validation / not_found 等) は **即失敗**: リクエスト内容が誤っている
# ので再送しても通らず、リトライは API へ無駄な負荷をかけるだけ。
_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

DEFAULT_RETRY_MAX_ATTEMPTS = 3
DEFAULT_RETRY_BASE_DELAY_SEC = 1.0
DEFAULT_RETRY_MAX_DELAY_SEC = 30.0


class GmoOrderClient:
    """GMOコイン Private API のうち、注文発火と約定確認に必要な POST/GET を扱う。"""

    def __init__(
        self,
        credentials: GmoCredentials,
        *,
        private_base: str = PRIVATE_BASE_URL,
        http_fn: HttpFn = _default_http,
        clock_fn: Callable[[], float] = _time.time,
        sleep_fn: Callable[[float], None] = _time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
        retry_max_attempts: int = DEFAULT_RETRY_MAX_ATTEMPTS,
        retry_base_delay_sec: float = DEFAULT_RETRY_BASE_DELAY_SEC,
        retry_max_delay_sec: float = DEFAULT_RETRY_MAX_DELAY_SEC,
    ) -> None:
        if credentials is None:
            raise ValueError("GmoOrderClient requires credentials")
        if retry_max_attempts < 1:
            raise ValueError("retry_max_attempts must be >= 1")
        self._credentials = credentials
        self._private_base = private_base.rstrip("/")
        self._http_fn = http_fn
        self._clock_fn = clock_fn
        self._sleep_fn = sleep_fn
        self._timeout = timeout
        self._retry_max_attempts = retry_max_attempts
        self._retry_base_delay_sec = retry_base_delay_sec
        self._retry_max_delay_sec = retry_max_delay_sec
        self._last_ts_ms: int = 0

    def __repr__(self) -> str:
        return f"GmoOrderClient(private_base={self._private_base!r})"

    def __str__(self) -> str:
        return self.__repr__()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _timestamp_ms(self) -> str:
        """ms 単位のタイムスタンプ。前回値より厳密に大きいことを保証する。

        GET-only クライアントと同形式 (nonce 衝突防止)。共有 util に切り出すかは
        テスト容易性とのトレードオフで判断、現状は複製のまま。
        """
        candidate = int(self._clock_fn() * 1000)
        if candidate <= self._last_ts_ms:
            candidate = self._last_ts_ms + 1
        self._last_ts_ms = candidate
        return str(candidate)

    def _signed_post(self, path: str, body_dict: dict[str, Any]) -> dict[str, Any]:
        """HMAC 署名付き POST。GMO は body を JSON 文字列にして sign に含める。

        retry 対象 (429 / 5xx / URLError) なら指数バックオフで再送。各試行で
        timestamp と sign を作り直す (古い ts は GMO 側で reject される可能性)。
        """
        # separators=(',',':')  — GMO サンプルと同じ最小 JSON 表現にしておくと、
        # 受信側で空白の有無による署名ミスを避けやすい
        body = json.dumps(body_dict, separators=(",", ":"))

        def build_req() -> urllib.request.Request:
            ts = self._timestamp_ms()
            signature = sign(self._credentials.api_secret, ts, "POST", path, body)
            url = f"{self._private_base}{path}"
            req = urllib.request.Request(
                url, data=body.encode("utf-8"), method="POST",
            )
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            req.add_header("API-KEY", self._credentials.api_key)
            req.add_header("API-TIMESTAMP", ts)
            req.add_header("API-SIGN", signature)
            logger.debug("gmo_order.post path=%s ts=%s", path, ts)
            return req

        return self._execute_with_retry(build_req, label=f"POST {path}")

    def _signed_get(self, path: str, params: dict[str, Any] | None) -> dict[str, Any]:
        """HMAC 署名付き GET。executions 確認用。

        retry 対象 (429 / 5xx / URLError) なら指数バックオフで再送。各試行で
        timestamp と sign を作り直す。
        """

        def build_req() -> urllib.request.Request:
            ts = self._timestamp_ms()
            signature = sign(self._credentials.api_secret, ts, "GET", path, "")
            full_path = path
            if params:
                full_path = f"{path}?{urllib.parse.urlencode(params)}"
            url = f"{self._private_base}{full_path}"
            # 署名対象 path は query string を含めない (GMO 仕様)
            req = urllib.request.Request(url, method="GET")
            req.add_header("Accept", "application/json")
            req.add_header("API-KEY", self._credentials.api_key)
            req.add_header("API-TIMESTAMP", ts)
            req.add_header("API-SIGN", signature)
            logger.debug("gmo_order.get path=%s ts=%s", path, ts)
            return req

        return self._execute_with_retry(build_req, label=f"GET {path}")

    def _execute_with_retry(
        self,
        build_req: Callable[[], urllib.request.Request],
        label: str,
    ) -> dict[str, Any]:
        """retry/backoff のループ本体。各 attempt で build_req() を呼び直す。

        ``build_req`` を毎回呼ぶことで、retry 時に新しい API-TIMESTAMP / API-SIGN
        を作り直す (古い ts は GMO 側で reject される設計のため)。
        """
        last_exc: GmoApiError | None = None
        for attempt in range(self._retry_max_attempts):
            req = build_req()
            try:
                return self._do_request(req)
            except GmoApiError as e:
                if not _is_retryable(e):
                    raise
                last_exc = e
                if attempt >= self._retry_max_attempts - 1:
                    break
                delay = min(
                    self._retry_base_delay_sec * (2 ** attempt),
                    self._retry_max_delay_sec,
                )
                logger.warning(
                    "gmo_order retry attempt=%d/%d label=%s status=%s delay=%.2fs",
                    attempt + 1, self._retry_max_attempts, label, e.status, delay,
                )
                self._sleep_fn(delay)
        # max_attempts 到達 — 最後の例外を raise
        assert last_exc is not None  # 上の for を 1 度は通る (max_attempts >= 1)
        raise last_exc

    def _do_request(self, req: urllib.request.Request) -> dict[str, Any]:
        """共通リクエスト実行とエラー集約。"""
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
    # 公開メソッド
    # ------------------------------------------------------------------
    def place_market_order(
        self,
        symbol: str,
        side: str,
        size_crypto: float,
        size_decimals: int,
    ) -> dict[str, Any]:
        """成行注文を 1 件出す。

        GMO 仕様で size は **暗号資産の数量** を文字列で渡す (BTC_JPY なら BTC 数量)。
        size_decimals に従って小数桁を切り捨てる (端数を丸めて GMO に拒否されないよう)。

        戻り値: ``{"order_id": <str>, "raw": <full response>}``。GMO は成功時 ``data``
        フィールドに orderId を string で返す仕様 (公式 docs より)。
        """
        side_upper = side.upper()
        if side_upper not in ("BUY", "SELL"):
            raise ValueError(f"side must be BUY or SELL, got {side!r}")
        if size_crypto <= 0:
            raise ValueError(f"size_crypto must be positive, got {size_crypto}")

        size_str = format_size(size_crypto, size_decimals)
        body = {
            "symbol": symbol,
            "side": side_upper,
            "executionType": "MARKET",
            "size": size_str,
        }
        payload = self._signed_post("/v1/order", body)
        order_id = _extract_order_id(payload)
        return {"order_id": order_id, "raw": payload}

    def get_executions_by_order_id(self, order_id: str) -> dict[str, Any]:
        """GET /v1/executions?orderId=XXX。約定確認用 (Phase 4b で利用予定)。"""
        return self._signed_get("/v1/executions", params={"orderId": order_id})


# ----------------------------------------------------------------------
# 補助関数 (純粋関数として export)
# ----------------------------------------------------------------------
def _is_retryable(err: GmoApiError) -> bool:
    """retry を許容する GmoApiError か。

    - status == 0  : URLError (接続失敗 / timeout) なら通信レイヤの一過性
    - status == 429: GMO レート制限 (バックオフで回復)
    - status in 5xx: GMO 側の一過性障害 (502, 503, 504 含む)
    - その他      : 4xx (auth / validation / not_found 等) や GMO の業務エラー
                    (insufficient balance 等) は再送しても通らないため即失敗
    """
    if err.status == 0:
        return True
    return err.status in _RETRYABLE_HTTP_STATUSES



def format_size(size_crypto: float, decimals: int) -> str:
    """size_crypto を GMO 受付形式 (固定小数文字列) に整形する。

    端数は **切り捨て** で丸める (上振れさせて拒否される/余分に約定するのを防ぐ)。
    decimals=0 のとき整数文字列を返す。
    """
    if decimals < 0:
        raise ValueError(f"decimals must be >= 0, got {decimals}")
    factor = 10 ** decimals
    truncated = int(size_crypto * factor) / factor
    if decimals == 0:
        return str(int(truncated))
    return f"{truncated:.{decimals}f}"


def extract_executions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """GMO executions レスポンスから execution 一覧を取り出す。

    GMO の戻り値は ``data: [...]`` (直値配列) と ``data: {"list": [...]}``
    (ページネーション形式) の両方があり得るため、どちらにも対応する。
    """
    data = raw.get("data")
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        lst = data.get("list")
        if isinstance(lst, list):
            return [e for e in lst if isinstance(e, dict)]
    return []


def sum_executions(executions: list[dict[str, Any]]) -> tuple[float, float]:
    """execution リストから (約定数量合計, 手数料合計) を返す。

    フィールドは GMO 仕様で ``executedSize`` (なければ ``size``) と ``fee``。
    どちらも文字列として返るためフロート化、欠損は 0 として扱う (個別の partial
    レスポンスでフィールドが揺れたケースに引っ張られないため)。
    """
    total_size = 0.0
    total_fee = 0.0
    for ex in executions:
        size_raw = ex.get("executedSize", ex.get("size"))
        fee_raw = ex.get("fee")
        if size_raw is not None:
            try:
                total_size += float(size_raw)
            except (TypeError, ValueError):
                pass
        if fee_raw is not None:
            try:
                total_fee += float(fee_raw)
            except (TypeError, ValueError):
                pass
    return total_size, total_fee


def _extract_order_id(payload: dict[str, Any]) -> str:
    """GMO レスポンスから orderId を取り出す。

    GMO 公式 docs では POST /v1/order のレスポンスは ``{"status":0,"data":"<orderId>"}``
    の形 (data 直値が string)。ただし他エンドポイントで ``data: {...}`` のケースも
    あるため、両対応で取り出す。
    """
    data = payload.get("data")
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        # 念のため辞書ケースもサポート (orderId キーで取れる場合)
        oid = data.get("orderId") or data.get("order_id")
        if oid is not None:
            return str(oid)
    if isinstance(data, (int, float)):
        return str(data)
    raise GmoApiError(
        status=-1,
        message=f"could not extract order_id from response: {payload!r}",
        payload=payload,
    )
