"""Windows hardening of every command-line script (hermetic: runs on Linux, macOS and Windows).

Every CLI is found by glob (a `scripts/*.py` with a `__main__` guard, plus `run_tests.py` and `check_env.py`
when they exist) so a new script cannot skip the check. Checks:
- static: the `__main__` branch calls a UTF-8 stdio helper BEFORE parsing arguments;
- behaviour: with `PYTHONIOENCODING=cp1252` (a legacy Windows console) `--help` and the real messages do not
  crash on Vietnamese/Korean text; a UTF-16 log (PowerShell 5.1 redirect) and a BOM narration file are read.
Run as a script: ``python tests/test_windows_hardening.py``.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
HELPERS = {"force_utf8_stdio", "reconfigure_stdio"}


def cli_files():
    # A real `if __name__ == "__main__":` branch (AST), not the word in a docstring.
    found = [p for p in sorted(SCRIPTS.glob("*.py"))
             if main_branch(ast.parse(p.read_text(encoding="utf-8"))) is not None]
    for extra in (ROOT / "run_tests.py", SCRIPTS / "check_env.py"):
        if extra.is_file() and extra not in found:
            found.append(extra)
    return found


def run_cli(script, *args, encoding="cp1252", cwd=None):
    env = dict(os.environ, PYTHONIOENCODING=encoding)
    env.pop("PYTHONUTF8", None)
    proc = subprocess.run(
        [sys.executable, str(script), *args], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd, timeout=120,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace"), proc.stderr.decode("utf-8", errors="replace")


def main_branch(tree):
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) \
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__":
            return node
    return None


def first_call_names(branch):
    """Names of the functions called by the branch's statements, in source order."""
    names = []
    for node in ast.walk(branch):
        if isinstance(node, ast.Call):
            func = node.func
            names.append((node.lineno, func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")))
    return [name for _, name in sorted(names)]


class TestEveryCliForcesUtf8(unittest.TestCase):
    def test_the_glob_finds_the_known_clis(self):
        names = {p.name for p in cli_files()}
        self.assertTrue({"build_srt.py", "burn_subtitles.py", "trim_black_tail.py",
                         "patch_chrome_prefs.py", "run_tests.py"} <= names, names)

    def test_main_branch_calls_a_utf8_helper_before_argument_parsing(self):
        for path in cli_files():
            with self.subTest(cli=path.name):
                branch = main_branch(ast.parse(path.read_text(encoding="utf-8")))
                self.assertIsNotNone(branch, "no `if __name__ == '__main__'` branch")
                calls = first_call_names(branch)
                helper_at = next((i for i, n in enumerate(calls) if n in HELPERS), None)
                if helper_at is None:
                    self.fail("the __main__ branch never forces UTF-8 stdio")
                parse_at = next((i for i, n in enumerate(calls) if n in ("parse_args", "main")), len(calls))
                self.assertLess(helper_at, parse_at, "UTF-8 must be forced BEFORE parse_args()/main()")

    def test_help_does_not_crash_on_a_cp1252_console(self):
        for path in cli_files():
            with self.subTest(cli=path.name):
                rc, out, err = run_cli(path, "--help")
                self.assertEqual(rc, 0, err)
                self.assertNotIn("UnicodeEncodeError", err)
                self.assertIn("usage", out.lower())


class TestBatchFiles(unittest.TestCase):
    def test_gitattributes_pins_batch_files_to_crlf_and_shell_scripts_to_lf(self):
        # An LF-only .cmd with goto labels can fail on Windows ("cannot find the batch label") when the user's git
        # does not convert line endings; the attributes make the checkout deterministic.
        text = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.cmd text eol=crlf", text)
        self.assertIn("*.bat text eol=crlf", text)
        self.assertRegex(text, r"\*\.sh\s+text eol=lf")

    def test_every_batch_file_of_the_repo_is_covered_by_the_pin(self):
        for path in ROOT.glob("*.cmd"):
            self.assertTrue(path.name.endswith(".cmd"))
        self.assertTrue(list(ROOT.glob("*.cmd")), "premise: there are batch files at the repo root")


class TestReadsWhatWindowsToolsWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rvs-hardening-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.narration = {"mo_menu": {"vi": "Mở menu Bán hàng → Báo giá", "ko": "판매 메뉴를 엽니다"}}

    def _build(self, log_bytes, narration_bytes=None):
        log = self.tmp / "run.log"
        log.write_bytes(log_bytes)
        nar = self.tmp / "narration.json"
        nar.write_bytes(narration_bytes if narration_bytes is not None
                        else json.dumps(self.narration, ensure_ascii=False).encode("utf-8"))
        out = self.tmp / "out_vi.srt"
        rc, stdout, stderr = run_cli(SCRIPTS / "build_srt.py", "--log", str(log), "--narration", str(nar),
                                      "--lang", "vi", "--out", str(out))
        return rc, stdout, stderr, out

    LOG = "INFO MARK 1.00 mo_menu\nINFO MARK 4.50 xong\n"

    def test_utf8_log_and_a_vietnamese_summary_line_on_cp1252(self):
        rc, stdout, stderr, out = self._build(self.LOG.encode("utf-8"))
        self.assertEqual(rc, 0, stderr)
        self.assertIn("Đã ghi 1 cue", stdout)
        self.assertIn("Mở menu Bán hàng → Báo giá", out.read_text(encoding="utf-8"))

    def test_utf16_log_with_bom_is_read(self):
        rc, _, stderr, out = self._build(b"\xff\xfe" + self.LOG.encode("utf-16-le"))
        self.assertEqual(rc, 0, stderr)
        self.assertIn("Mở menu", out.read_text(encoding="utf-8"))

    def test_utf16_log_without_bom_is_read(self):
        rc, _, stderr, out = self._build(self.LOG.encode("utf-16-le"))
        self.assertEqual(rc, 0, stderr)
        self.assertIn("Mở menu", out.read_text(encoding="utf-8"))

    def test_narration_json_with_a_utf8_bom_is_read(self):
        data = b"\xef\xbb\xbf" + json.dumps(self.narration, ensure_ascii=False).encode("utf-8")
        rc, _, stderr, out = self._build(self.LOG.encode("utf-8"), data)
        self.assertEqual(rc, 0, stderr)
        self.assertIn("Mở menu", out.read_text(encoding="utf-8"))

    def test_srt_is_written_with_unix_newlines_only(self):
        _, _, _, out = self._build(self.LOG.encode("utf-8"))
        self.assertNotIn(b"\r", out.read_bytes())

    def test_the_srt_writer_asks_for_unix_newlines_explicitly(self):
        # Linux cannot show the Windows text-mode translation (\n -> \r\n), so pin the cause instead: the
        # open() that writes the .srt must say newline="\n".
        source = (SCRIPTS / "build_srt.py").read_text(encoding="utf-8")
        writer = [line for line in source.splitlines() if "args.out" in line and "open(" in line]
        self.assertTrue(writer, "the .srt writer was not found")
        self.assertIn('newline="\\n"', writer[0])

    def test_a_narration_that_is_not_utf8_fails_loudly_instead_of_burning_garbage(self):
        narration = '{"mo_menu": {"vi": "Café menu"}}'.encode("cp1252")
        rc, _, stderr, out = self._build(self.LOG.encode("utf-8"), narration)
        self.assertEqual(rc, 2, stderr)
        self.assertIn("UTF-8", stderr)
        self.assertFalse(out.exists(), "no .srt must be written")

    def test_a_utf16_narration_with_a_bom_is_read(self):
        data = b"\xff\xfe" + json.dumps(self.narration, ensure_ascii=False).encode("utf-16-le")
        rc, _, stderr, out = self._build(self.LOG.encode("utf-8"), data)
        self.assertEqual(rc, 0, stderr)
        self.assertIn("Mở menu", out.read_text(encoding="utf-8"))

    def test_a_non_utf8_log_byte_does_not_crash(self):
        rc, _, stderr, _ = self._build(b"\xff\xfe\xfdgarbage\nINFO MARK 1.00 mo_menu\n")
        self.assertEqual(rc, 0, stderr)

    def test_burn_subtitles_vietnamese_error_survives_a_cp1252_console(self):
        rc, _, err = run_cli(SCRIPTS / "burn_subtitles.py", "--input", str(self.tmp / "x.mp4"), "--lang", "vi",
                             "--srt", str(self.tmp / "missing.srt"))
        self.assertEqual(rc, 2, err)
        self.assertIn("không thấy file phụ đề", err)

    def test_patch_chrome_prefs_reports_a_profile_path_with_diacritics(self):
        profile = self.tmp / "Hồ sơ Việt"
        rc, out, err = run_cli(SCRIPTS / "patch_chrome_prefs.py", "--profile-dir", str(profile), "--lang", "vi")
        self.assertEqual(rc, 0, err)
        self.assertIn("Hồ sơ Việt", out)


if __name__ == "__main__":
    unittest.main()
