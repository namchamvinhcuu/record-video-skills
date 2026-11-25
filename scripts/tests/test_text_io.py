#!/usr/bin/env python3
"""Tests for text_io.py — hermetic; every case writes its own temp file.

Covers every encoding branch of `read_text_sniffed` (UTF-8, UTF-8 BOM, UTF-16 LE/BE with a BOM, BOM-less UTF-16-LE),
the lenient default (U+FFFD, for logs) versus `strict=True` (raises, for text that becomes a subtitle) and
`force_utf8_stdio` with and without `reconfigure`.

Run: python3 scripts/tests/test_text_io.py
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import text_io  # noqa: E402

TEXT = "Mở menu Bán hàng → 판매 메뉴"


class TestReadTextSniffed(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)

    def read(self, data: bytes, **kw) -> str:
        path = self.dir / "f.txt"
        path.write_bytes(data)
        return text_io.read_text_sniffed(path, **kw)

    def test_each_encoding_round_trips(self):
        cases = {
            "utf-8": TEXT.encode("utf-8"),
            "utf-8 bom": b"\xef\xbb\xbf" + TEXT.encode("utf-8"),
            "utf-16 le bom": b"\xff\xfe" + TEXT.encode("utf-16-le"),
            "utf-16 be bom": b"\xfe\xff" + TEXT.encode("utf-16-be"),
            "utf-16 le no bom": TEXT.encode("utf-16-le"),
        }
        for name, data in cases.items():
            for strict in (False, True):
                with self.subTest(encoding=name, strict=strict):
                    self.assertEqual(self.read(data, strict=strict), TEXT)

    def test_utf16_be_is_only_reachable_through_its_bom(self):
        # no BOM and big-endian would be misread as little-endian: the BOM branch is what makes BE work
        self.assertEqual(self.read(b"\xfe\xff" + "MARK 1.0 a".encode("utf-16-be")), "MARK 1.0 a")

    def test_lenient_default_turns_bad_bytes_into_replacement_characters(self):
        out = self.read("Café".encode("cp1252"))
        self.assertIn("�", out)
        self.assertTrue(out.startswith("Caf"))

    def test_strict_raises_on_bad_bytes_in_every_branch(self):
        bad = {
            "utf-8": "Café".encode("cp1252"),
            "utf-8 bom": b"\xef\xbb\xbf" + "Café".encode("cp1252"),
            "utf-16 bom": b"\xff\xfe" + b"a\x00\x00\xd8",   # a lone high surrogate
            "utf-16 no bom": b"a\x00\x00\xd8",              # same payload through the NUL-sniffing branch
        }
        for name, data in bad.items():
            with self.subTest(branch=name), self.assertRaises(UnicodeDecodeError):
                self.read(data, strict=True)

    def test_empty_file(self):
        self.assertEqual(self.read(b""), "")
        self.assertEqual(self.read(b"", strict=True), "")


class TestForceUtf8Stdio(unittest.TestCase):
    def test_reconfigures_streams_that_support_it(self):
        calls = []

        class Stream(io.StringIO):
            def reconfigure(self, **kw):
                calls.append(kw)

        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = Stream(), Stream()
        try:
            text_io.force_utf8_stdio()
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(calls, [{"encoding": "utf-8", "errors": "replace"}] * 2)

    def test_a_stream_without_reconfigure_is_left_alone(self):
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            text_io.force_utf8_stdio()      # must not raise
        finally:
            sys.stdout, sys.stderr = old


if __name__ == "__main__":
    unittest.main()
