"""ログ出力フィルタ。

API キー・シークレットなどが万が一ログレコードに乗っても出力される前に
`***` に置換するセーフティネット。攻撃面を減らすために handler に attach する。

CLAUDE.md Hard Rule #2「API キー・秘密情報をコード・ログ・コミットに出さない」
を担保する最後の防波堤。
"""
from __future__ import annotations

import logging
from typing import Iterable


class SecretMaskFilter(logging.Filter):
    """指定文字列を record の msg / args 中で `***` に置換する。

    - 空文字列は無視する（env 未設定で全てが伏字になる事故を防ぐ）
    - record の論理（msg + args）を直接書き換える。Formatter を通る前に消える
    - 常に True を返す。記録自体は通す
    """

    def __init__(self, secrets: Iterable[str], replacement: str = "***") -> None:
        super().__init__()
        self._secrets: list[str] = [s for s in secrets if s]
        self._replacement = replacement

    def _mask(self, value: object) -> object:
        if isinstance(value, str):
            out = value
            for s in self._secrets:
                if s in out:
                    out = out.replace(s, self._replacement)
            return out
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True

        if isinstance(record.msg, str):
            record.msg = self._mask(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._mask(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._mask(v) for v in record.args)

        return True
