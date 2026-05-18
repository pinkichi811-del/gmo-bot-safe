"""GMOコイン private API の疎通確認スクリプト (read-only)。

`.env` に `GMO_API_KEY` / `GMO_API_SECRET` が設定されている前提で、
`GET /v1/account/assets` を叩いて残高を表示する。

実行方法:
    cd ~/gmo-bot-safe
    .venv/bin/python scripts/probe_gmo_assets.py

このスクリプトは Hard Rule 厳守:
- read-only エンドポイントのみ
- 注文系メソッドを呼ばない (そもそも GmoApiClient に存在しない)
- API キーは出力しない (logger filter + dataclass __repr__ で二重に保護)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# .env を最小ロード (PyYAML を使わない、外部依存なし)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # 既に環境にあれば上書きしない (systemd 等で先に置かれている可能性)
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    _load_dotenv(ROOT / ".env")

    from gmo_api_client import GmoApiClient, GmoApiError  # noqa: E402

    try:
        client = GmoApiClient.from_env()
    except ValueError as e:
        print(f"[FAIL] env not configured: {e}", file=sys.stderr)
        print("  .env に GMO_API_KEY / GMO_API_SECRET を設定してください。", file=sys.stderr)
        return 2

    print(f"[INFO] client = {client!r}")
    print("[INFO] requesting GET /private/v1/account/assets ...")

    try:
        payload = client.get_account_assets()
    except GmoApiError as e:
        print(f"[FAIL] API error status={e.status} message={e.message}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] unexpected error: {e}", file=sys.stderr)
        return 4

    print("[OK] response received.")
    # GMO の応答は {"status":0,"data":[{"asset":"JPY","amount":"...","available":"...",...}, ...]}
    data = payload.get("data") or []
    if not data:
        print("[INFO] no assets found.")
        return 0

    print(f"[INFO] {len(data)} asset rows:")
    for row in data:
        # 個々の row は全部表示。シークレットは含まれないので問題ない。
        print(f"  {json.dumps(row, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
