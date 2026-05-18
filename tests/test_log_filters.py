"""SecretMaskFilter のテスト。

実 secret 風文字列が record に乗っても formatter 前に `***` に置換されることを
確認する。self.assertLogs は内部 handler を別に挿入してしまうので、本番と同じ
「handler に filter を attach する」経路を測れるよう自前の capturing handler
を使う。
"""
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from log_filters import SecretMaskFilter  # noqa: E402


class _CapturingHandler(logging.Handler):
    """emit された record を保持し、format 済み文字列を返す。"""

    def __init__(self) -> None:
        super().__init__()
        self.formatted: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.formatted.append(self.format(record))


def _setup(name: str, secrets: list[str]) -> tuple[logging.Logger, _CapturingHandler]:
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.filters.clear()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    handler = _CapturingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SecretMaskFilter(secrets))
    logger.addHandler(handler)
    return logger, handler


class TestSecretMaskFilter(unittest.TestCase):
    def test_mask_replaces_secret_in_msg(self) -> None:
        logger, handler = _setup("t1", ["SECRETVAL"])
        logger.info("key=SECRETVAL leaked")
        output = "\n".join(handler.formatted)
        self.assertNotIn("SECRETVAL", output)
        self.assertIn("***", output)

    def test_mask_replaces_secret_in_args(self) -> None:
        logger, handler = _setup("t2", ["SECRETVAL"])
        logger.info("key=%s", "SECRETVAL")
        output = "\n".join(handler.formatted)
        self.assertNotIn("SECRETVAL", output)
        self.assertIn("***", output)

    def test_mask_replaces_in_dict_args(self) -> None:
        logger, handler = _setup("t3", ["SECRETVAL"])
        logger.info("key=%(k)s", {"k": "SECRETVAL"})
        output = "\n".join(handler.formatted)
        self.assertNotIn("SECRETVAL", output)

    def test_mask_empty_secret_noop(self) -> None:
        logger, handler = _setup("t4", [""])
        logger.info("nothing to mask")
        output = "\n".join(handler.formatted)
        self.assertIn("nothing to mask", output)
        self.assertNotIn("***", output)

    def test_mask_non_str_args_safe(self) -> None:
        logger, handler = _setup("t5", ["SECRETVAL"])
        logger.info("count=%d ratio=%.2f flag=%s", 3, 0.5, "SECRETVAL")
        output = "\n".join(handler.formatted)
        self.assertNotIn("SECRETVAL", output)
        self.assertIn("count=3", output)
        self.assertIn("0.50", output)

    def test_mask_in_exception_message(self) -> None:
        logger, handler = _setup("t6", ["SECRETVAL"])
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logger.exception("caught with key=%s", "SECRETVAL")
        output = "\n".join(handler.formatted)
        # exception の traceback 自体には RuntimeError("boom") しか乗らないので、
        # ここでは msg + args (= 第一行) が masked であることだけ確認する。
        first_line = output.splitlines()[0] if output else ""
        self.assertNotIn("SECRETVAL", first_line)

    def test_multiple_secrets_all_masked(self) -> None:
        logger, handler = _setup("t7", ["AAA", "BBB"])
        logger.info("first=AAA second=BBB")
        output = "\n".join(handler.formatted)
        self.assertNotIn("AAA", output)
        self.assertNotIn("BBB", output)


if __name__ == "__main__":
    unittest.main()
