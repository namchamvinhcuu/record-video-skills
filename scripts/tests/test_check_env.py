#!/usr/bin/env python3
"""Tests for check_env.py — hermetic: every probe is injected (fake `which`, fake `run`, fake disk usage), so the
file gives the same result on Windows, macOS and Linux. Covers the Microsoft Store `python` stub (exit 9009),
a missing launcher, missing libass / libx264, the Playwright package-vs-Chromium split, fonts per platform, the
disk threshold, the report format and the exit code. Nothing is written to disk.

Run: python3 scripts/tests/test_check_env.py
"""
from __future__ import annotations

import subprocess
import sys
import types
import unittest
from collections import namedtuple
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_env as ce  # noqa: E402


def proc(returncode=0, out=b""):
    return types.SimpleNamespace(returncode=returncode, stdout=out)


def which_only(*names):
    return lambda name: f"C:/bin/{name}.exe" if name in names else None


class TestWingetCommands(unittest.TestCase):
    def test_every_winget_command_accepts_the_agreements_up_front(self):
        # a first winget run in a non-interactive shell would otherwise wait for a prompt nobody can answer
        for command in (ce.WINGET_PYTHON, ce.WINGET_FFMPEG):
            self.assertIn("--accept-source-agreements", command)
            self.assertIn("--accept-package-agreements", command)
            self.assertTrue(command.startswith("winget install "))


class TestPython(unittest.TestCase):
    def test_new_enough(self):
        self.assertTrue(ce.check_python((3, 12, 1)).ok)
        self.assertTrue(ce.check_python((3, 10, 0)).ok)

    def test_too_old_names_the_install_command(self):
        c = ce.check_python((3, 9, 6))
        self.assertFalse(c.ok)
        self.assertIn("winget install Python.Python.3.12", c.fix)


class TestFindPython(unittest.TestCase):
    def test_py_launcher_wins(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return proc(0, b"Python 3.12.4\n")
        c = ce.find_python(which=which_only("py", "python"), run=run)
        self.assertTrue(c.ok)
        self.assertEqual(calls, [["py", "-3", "--version"]])

    def test_the_store_stub_exits_9009_and_counts_as_missing(self):
        stub = b"Python was not found; run without arguments to install from the Microsoft Store"
        c = ce.find_python(which=which_only("python"), run=lambda cmd, **kw: proc(9009, stub))
        self.assertFalse(c.ok)
        self.assertIn("winget install Python.Python.3.12", c.fix)

    def test_a_printed_version_does_not_count_when_the_exit_code_is_nonzero(self):
        c = ce.find_python(which=which_only("python"), run=lambda cmd, **kw: proc(9009, b"Python 3.12.0\n"))
        self.assertFalse(c.ok)

    def test_a_stub_that_exits_zero_but_prints_no_version_is_missing(self):
        c = ce.find_python(which=which_only("python"), run=lambda cmd, **kw: proc(0, b"hello"))
        self.assertFalse(c.ok)

    def test_too_old_version_is_rejected_and_the_next_candidate_is_tried(self):
        outputs = {"py": proc(0, b"Python 3.8.10\n"), "python": proc(0, b"Python 3.11.2\n")}
        c = ce.find_python(which=which_only("py", "python"), run=lambda cmd, **kw: outputs[cmd[0]])
        self.assertTrue(c.ok)
        self.assertIn("python", c.detail)

    def test_nothing_on_path(self):
        c = ce.find_python(which=which_only(), run=lambda cmd, **kw: self.fail("must not run anything"))
        self.assertFalse(c.ok)

    def test_a_launcher_that_cannot_start_is_skipped(self):
        def run(cmd, **kw):
            raise OSError("exec format error")
        self.assertFalse(ce.find_python(which=which_only("py", "python", "python3"), run=run).ok)

    def test_never_uses_where(self):
        seen = []
        ce.find_python(which=lambda n: seen.append(n) or None, run=lambda cmd, **kw: proc())
        self.assertNotIn("where", seen)


class TestPlaywright(unittest.TestCase):
    def test_package_missing(self):
        c = ce.check_playwright(find_spec=lambda name: None)
        self.assertFalse(c.ok)
        self.assertIn("pip install playwright", c.fix)
        self.assertIn("playwright install chromium", c.fix)

    def test_package_present_chromium_missing(self):
        c = ce.check_playwright(find_spec=lambda name: object(), chromium_path=lambda: "/no/such/chrome")
        self.assertFalse(c.ok)
        self.assertEqual(c.fix, "py -3 -m playwright install chromium")

    def test_everything_present(self):
        c = ce.check_playwright(find_spec=lambda name: object(), chromium_path=lambda: sys.executable)
        self.assertTrue(c.ok)

    def test_a_driver_failure_is_a_readable_missing_not_a_crash(self):
        def boom():
            raise RuntimeError("driver exploded")
        c = ce.check_playwright(find_spec=lambda name: object(), chromium_path=boom)
        self.assertFalse(c.ok)
        self.assertIn("RuntimeError", c.detail)


FILTERS_WITH = " ... subtitles         V->V       Render text subtitles onto input video using the libass library.\n"
FILTERS_WITHOUT = " ... drawtext          V->V       Draw text on top of video frames.\n"
ENCODERS_WITH = " V....D libx264              libx264 H.264 / AVC (codec h264)\n"
ENCODERS_WITHOUT = " V....D mpeg4               MPEG-4 part 2\n"


def ffmpeg_run(filters, encoders):
    return lambda cmd, **kw: proc(0, (filters if "-filters" in cmd else encoders).encode("utf-8"))


class TestFfmpeg(unittest.TestCase):
    def test_not_on_path(self):
        c = ce.check_ffmpeg(which=which_only(), run=ffmpeg_run("", ""))
        self.assertFalse(c.ok)
        self.assertIn("winget install Gyan.FFmpeg", c.fix)

    def test_full_build(self):
        self.assertTrue(ce.check_ffmpeg(which=which_only("ffmpeg"), run=ffmpeg_run(FILTERS_WITH, ENCODERS_WITH)).ok)

    def test_no_libass(self):
        c = ce.check_ffmpeg(which=which_only("ffmpeg"), run=ffmpeg_run(FILTERS_WITHOUT, ENCODERS_WITH))
        self.assertFalse(c.ok)
        self.assertIn("libass", c.detail)

    def test_no_libx264(self):
        c = ce.check_ffmpeg(which=which_only("ffmpeg"), run=ffmpeg_run(FILTERS_WITH, ENCODERS_WITHOUT))
        self.assertFalse(c.ok)
        self.assertIn("libx264", c.detail)

    def test_cannot_start(self):
        def run(cmd, **kw):
            raise FileNotFoundError("ffmpeg")
        c = ce.check_ffmpeg(which=which_only("ffmpeg"), run=run)
        self.assertFalse(c.ok)

    def test_unreadable_listing_is_a_skip_not_a_pass(self):
        c = ce.check_ffmpeg(which=which_only("ffmpeg"), run=lambda cmd, **kw: proc(1, None))
        self.assertIsNone(c.ok)


class TestPillowAndFfprobe(unittest.TestCase):
    def test_pillow_missing_names_the_pip_command_on_the_linux_capture(self):
        c = ce.check_pillow(find_spec=lambda name: None, platform="linux")
        self.assertFalse(c.ok)
        self.assertIn("pip install pillow", c.fix)

    def test_pillow_and_ffprobe_are_only_informational_on_the_playwright_video_path(self):
        for platform in ("win32", "darwin"):
            with self.subTest(platform=platform):
                p = ce.check_pillow(find_spec=lambda name: None, platform=platform)
                f = ce.check_ffprobe(which=which_only(), platform=platform)
                self.assertIsNone(p.ok)
                self.assertIsNone(f.ok)
                self.assertEqual((p.fix, f.fix), ("", ""))
                self.assertTrue(ce.check_pillow(find_spec=lambda name: object(), platform=platform).ok)
                self.assertTrue(ce.check_ffprobe(which=which_only("ffprobe"), platform=platform).ok)

    def test_pillow_present(self):
        self.assertTrue(ce.check_pillow(find_spec=lambda name: object()).ok)

    def test_the_pillow_probe_asks_for_the_PIL_module(self):
        asked = []
        ce.check_pillow(find_spec=lambda name: asked.append(name) or object())
        self.assertEqual(asked, ["PIL"])

    def test_ffprobe_missing_and_present(self):
        self.assertFalse(ce.check_ffprobe(which=which_only(), platform="linux").ok)
        self.assertIn("winget install Gyan.FFmpeg", ce.check_ffprobe(which=which_only(), platform="linux").fix)
        self.assertTrue(ce.check_ffprobe(which=which_only("ffprobe"), platform="linux").ok)

    def test_the_report_covers_them_so_ready_cannot_hide_a_trim_crash(self):
        import io
        import contextlib
        seen = []
        orig = {n: getattr(ce, n) for n in ("check_python", "find_python", "check_playwright", "check_pillow",
                                            "check_ffmpeg", "check_ffprobe", "check_font", "check_disk")}
        try:
            for name in orig:
                setattr(ce, name, lambda name=name: seen.append(name) or ce.Check(name, True, "ok"))
            with contextlib.redirect_stdout(io.StringIO()):
                ce.main([])
        finally:
            for name, fn in orig.items():
                setattr(ce, name, fn)
        self.assertIn("check_pillow", seen)
        self.assertIn("check_ffprobe", seen)


class TestFont(unittest.TestCase):
    def test_windows_with_malgun_gothic(self):
        c = ce.check_font(platform="win32", families_fn=lambda: {"Malgun Gothic", "Segoe UI"})
        self.assertTrue(c.ok)
        self.assertIn("Malgun Gothic", c.detail)

    def test_windows_without_a_korean_font(self):
        c = ce.check_font(platform="win32", families_fn=lambda: {"Segoe UI", "Arial"})
        self.assertFalse(c.ok)
        self.assertTrue(c.fix)

    def test_other_platform_without_fontconfig_is_a_skip(self):
        self.assertIsNone(ce.check_font(platform="linux", which=which_only()).ok)

    def test_other_platform_with_a_korean_family(self):
        c = ce.check_font(platform="linux", which=which_only("fc-list"),
                          run=lambda cmd, **kw: proc(0, "Noto Sans CJK KR\n".encode("utf-8")))
        self.assertTrue(c.ok)

    def test_other_platform_with_no_korean_family(self):
        c = ce.check_font(platform="linux", which=which_only("fc-list"), run=lambda cmd, **kw: proc(0, b""))
        self.assertFalse(c.ok)


class TestDisk(unittest.TestCase):
    Usage = namedtuple("Usage", "total used free")

    def test_enough(self):
        c = ce.check_disk("/x", usage=lambda p: self.Usage(100 * 1024 ** 3, 0, 50 * 1024 ** 3))
        self.assertTrue(c.ok)

    def test_too_little(self):
        c = ce.check_disk("/x", usage=lambda p: self.Usage(100 * 1024 ** 3, 0, int(0.5 * 1024 ** 3)))
        self.assertFalse(c.ok)

    def test_exactly_the_minimum_is_enough(self):
        gb = 1024 ** 3
        self.assertTrue(ce.check_disk("/x", min_gb=2, usage=lambda p: self.Usage(10 * gb, 0, 2 * gb)).ok)
        self.assertFalse(ce.check_disk("/x", min_gb=2, usage=lambda p: self.Usage(10 * gb, 0, 2 * gb - 1)).ok)

    def test_unreadable_is_a_skip(self):
        def usage(p):
            raise OSError("no such path")
        self.assertIsNone(ce.check_disk("/x", usage=usage).ok)


class TestReport(unittest.TestCase):
    CHECKS = [ce.Check("python", True, "Python 3.12"),
              ce.Check("ffmpeg", False, "ffmpeg is not on PATH", "winget install Gyan.FFmpeg"),
              ce.Check("font", None, "not checked")]

    def test_lines_and_fix_and_summary(self):
        text = ce.format_report(self.CHECKS)
        self.assertIn("OK      python: Python 3.12", text)
        self.assertIn("MISSING ffmpeg: ffmpeg is not on PATH", text)
        self.assertIn("fix: winget install Gyan.FFmpeg", text)
        self.assertIn("SKIP    font: not checked", text)
        self.assertTrue(text.endswith("NOT READY: 1 prerequisite(s) MISSING."))

    def test_ready_when_nothing_is_missing(self):
        text = ce.format_report([ce.Check("python", True, "ok"), ce.Check("font", None, "skip")])
        self.assertTrue(text.endswith("READY: all prerequisites found."))
        self.assertNotIn("fix:", text)

    def test_a_skip_never_prints_a_fix(self):
        self.assertNotIn("fix:", ce.format_report([ce.Check("font", None, "skip", "some fix")]))


class TestMain(unittest.TestCase):
    def test_exit_code_follows_missing_items(self):
        import contextlib
        import io
        for checks, want in (([ce.Check("a", True, "x")], 0), ([ce.Check("a", None, "x")], 0),
                             ([ce.Check("a", False, "x", "fix")], 1)):
            with self.subTest(checks=checks):
                orig = ce.run_checks
                ce.run_checks = lambda checks=checks: checks
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(ce.main([]), want)
                finally:
                    ce.run_checks = orig

    def test_runs_as_a_script_and_prints_a_report(self):
        out = subprocess.run([sys.executable, str(SCRIPTS_DIR / "check_env.py")], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        text = out.stdout.decode("utf-8", errors="replace")
        self.assertIn("python:", text)
        self.assertIn("ffmpeg:", text)
        self.assertIn(out.returncode, (0, 1))
        self.assertEqual(out.returncode == 0, text.rstrip().endswith("READY: all prerequisites found."))


if __name__ == "__main__":
    unittest.main()
