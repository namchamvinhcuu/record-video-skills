#!/usr/bin/env python3
"""Test `DemoRecorder.session()` — KHÔNG cần Xvfb/ffmpeg/Chrome/Playwright thật. Mock
`playwright.async_api` bằng module giả (đặt trong sys.modules TRƯỚC khi gọi session() —
recorder.py import `playwright.async_api` LAZY bên trong hàm nên không cần cài package
thật) + patch instance method start/stop để không spawn process thật.

REGRESSION cho bug 🟠 Major: nếu
`launch_persistent_context()` raise (path Chrome sai, profile lock tranh chấp, Chrome
crash lúc khởi động), `stop_ffmpeg()`/`stop_xvfb()` PHẢI vẫn được gọi — thiếu try/finality
đúng chỗ làm Xvfb + ffmpeg leak chạy nền vô thời hạn (ffmpeg tiếp tục ghi màn hình đen).

Chạy: python3 shared/skills/record-demo-video/scripts/tests/test_recorder.py
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent

# Hermetic: the REAL playwright is never importable in this file, so a session() test that forgets the fake
# `playwright.async_api` fails on EVERY machine — not only on one without playwright (regression
# macOS CLT python 3.9: test_start_xvfb_failure_does_not_call_stop_ffmpeg passed on pyenv 3.11 only because
# playwright was installed there, and raised ModuleNotFoundError under macOS CLT python 3.9). A fake placed
# at sys.modules["playwright.async_api"] is still found: the import machinery checks the full name first.
sys.modules["playwright"] = None

# recorder.py làm `from click_helpers import CursorState` / `from patch_chrome_prefs import
# patch_chrome_prefs` như top-level import (cùng thư mục scripts/) — cần scripts/ trên
# sys.path để import machinery tìm ra 2 module anh em này.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SCRIPT = SCRIPTS_DIR / "recorder.py"
spec = importlib.util.spec_from_file_location("recorder_mod", SCRIPT)
assert spec and spec.loader
recorder_mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recorder_mod
spec.loader.exec_module(recorder_mod)

# start_ffmpeg() opens `<out_path>.ffmpeg.log` for real even with a fake Popen: every out_path of this
# file lives in its OWN temp dir (a shared /tmp/unused.mp4.ffmpeg.log was left behind by every run).
_OUT_DIR = tempfile.mkdtemp(prefix="rdv-test-recorder-")
atexit.register(shutil.rmtree, _OUT_DIR, True)
UNUSED_MP4 = os.path.join(_OUT_DIR, "unused.mp4")


class _FakeChromiumRaises:
    async def launch_persistent_context(self, **kw):
        raise RuntimeError("simulated Chrome launch failure (profile lock/path sai/crash)")


class _FakePlaywrightCM:
    """Giả `async_playwright()` — chỉ cần đủ để `async with async_playwright() as p:` chạy
    được và `p.chromium` trả object có `launch_persistent_context` raise."""

    def __init__(self, chromium):
        self._chromium = chromium

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    @property
    def chromium(self):
        return self._chromium


class _FakeCtx:
    """Giả `BrowserContext` — `launch_persistent_context()` THÀNH CÔNG (khác test trên),
    nhưng bước SAU đó (`add_init_script`) raise. Đây là lớp try/finally TRONG CÙNG
    (`finally: await ctx.close()`) — chưa test nào ở trên chạm tới vì cả 2 đều raise
    TRƯỚC KHI ctx được tạo."""

    def __init__(self, calls):
        self._calls = calls
        self.pages: list = []

    async def add_init_script(self, path=None):
        raise RuntimeError("simulated cursor_overlay.js injection failure")

    async def close(self):
        self._calls.append("ctx_close")


class _FakeChromiumInitScriptFails:
    def __init__(self, calls):
        self._calls = calls

    async def launch_persistent_context(self, **kw):
        return _FakeCtx(self._calls)


class TestSessionCleanupOnLaunchFailure(unittest.TestCase):
    def test_stop_ffmpeg_and_xvfb_called_when_launch_raises(self):
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as profile_dir:
            rec = recorder_mod.DemoRecorder(display=":99", out_path=UNUSED_MP4, profile_dir=profile_dir,
                                            capture="x11")
            # Patch instance method — KHÔNG spawn Xvfb/ffmpeg thật, chỉ ghi lại thứ tự gọi.
            rec.start_xvfb = lambda: calls.append("start_xvfb")
            rec.start_ffmpeg = lambda: calls.append("start_ffmpeg")
            rec.stop_ffmpeg = lambda: calls.append("stop_ffmpeg")
            rec.stop_xvfb = lambda: calls.append("stop_xvfb")

            fake_async_api = types.SimpleNamespace(
                async_playwright=lambda: _FakePlaywrightCM(_FakeChromiumRaises())
            )

            async def run():
                sys.modules["playwright.async_api"] = fake_async_api
                try:
                    async with rec.session():
                        self.fail("không bao giờ tới yield — launch_persistent_context phải raise trước")
                finally:
                    del sys.modules["playwright.async_api"]

            with self.assertRaises(RuntimeError):
                asyncio.run(run())

        self.assertIn("start_xvfb", calls)
        self.assertIn("start_ffmpeg", calls)
        self.assertIn("stop_ffmpeg", calls, "ffmpeg PHẢI dừng dù Chrome launch fail — thiếu = resource leak")
        self.assertIn("stop_xvfb", calls, "Xvfb PHẢI dừng dù Chrome launch fail — thiếu = resource leak")
        # Thứ tự dừng phải NGƯỢC thứ tự khởi (ffmpeg trước Xvfb — process con dừng trước process cha).
        self.assertLess(calls.index("stop_ffmpeg"), calls.index("stop_xvfb"))

    def test_start_xvfb_failure_does_not_call_stop_ffmpeg(self):
        """start_xvfb() tự nó raise (Xvfb binary thiếu) → chưa từng start_ffmpeg → không nên
        gọi stop_ffmpeg (ffmpeg chưa từng chạy)."""
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as profile_dir:
            rec = recorder_mod.DemoRecorder(display=":99", out_path=UNUSED_MP4, profile_dir=profile_dir,
                                            capture="x11")

            def _raise_xvfb():
                calls.append("start_xvfb")
                raise FileNotFoundError("Xvfb binary không có trên máy này")

            rec.start_xvfb = _raise_xvfb
            rec.start_ffmpeg = lambda: calls.append("start_ffmpeg")
            rec.stop_ffmpeg = lambda: calls.append("stop_ffmpeg")
            rec.stop_xvfb = lambda: calls.append("stop_xvfb")

            # session() imports playwright BEFORE start_xvfb — without the fake, a machine with no
            # playwright raises ModuleNotFoundError here instead of the FileNotFoundError under test.
            fake_async_api = types.SimpleNamespace(
                async_playwright=lambda: _FakePlaywrightCM(_FakeChromiumRaises())
            )

            async def run():
                sys.modules["playwright.async_api"] = fake_async_api
                try:
                    async with rec.session():
                        self.fail("không bao giờ tới yield — start_xvfb phải raise trước")
                finally:
                    del sys.modules["playwright.async_api"]

            with self.assertRaises(FileNotFoundError):
                asyncio.run(run())

        self.assertEqual(calls, ["start_xvfb"], "start_ffmpeg/stop_ffmpeg/stop_xvfb không được gọi khi Xvfb chưa từng start")

    def test_ctx_closed_and_stop_called_when_add_init_script_raises(self):
        """Chrome đã launch THÀNH CÔNG (khác 2 test trên) nhưng add_init_script (inject
        cursor_overlay.js) raise NGAY SAU — lớp try/finally trong cùng (`finally: await
        ctx.close()`) phải chạy, và 2 lớp ngoài (stop_ffmpeg/stop_xvfb) vẫn phải chạy theo
        như cũ. Case này KHÁC 2 test trên: ở đó launch_persistent_context/start_xvfb raise
        TRƯỚC KHI ctx tồn tại nên ctx.close() chưa từng được thực thi trong test nào."""
        calls: list[str] = []
        with tempfile.TemporaryDirectory() as profile_dir:
            rec = recorder_mod.DemoRecorder(display=":99", out_path=UNUSED_MP4, profile_dir=profile_dir,
                                            capture="x11")
            rec.start_xvfb = lambda: calls.append("start_xvfb")
            rec.start_ffmpeg = lambda: calls.append("start_ffmpeg")
            rec.stop_ffmpeg = lambda: calls.append("stop_ffmpeg")
            rec.stop_xvfb = lambda: calls.append("stop_xvfb")

            fake_async_api = types.SimpleNamespace(
                async_playwright=lambda: _FakePlaywrightCM(_FakeChromiumInitScriptFails(calls))
            )

            async def run():
                sys.modules["playwright.async_api"] = fake_async_api
                try:
                    async with rec.session():
                        self.fail("không bao giờ tới yield — add_init_script phải raise trước")
                finally:
                    del sys.modules["playwright.async_api"]

            with self.assertRaises(RuntimeError):
                asyncio.run(run())

        self.assertIn("ctx_close", calls, "ctx.close() PHẢI được gọi dù add_init_script raise — leak tiến trình Chrome")
        self.assertIn("stop_ffmpeg", calls)
        self.assertIn("stop_xvfb", calls)
        self.assertLess(calls.index("ctx_close"), calls.index("stop_ffmpeg"),
                         "ctx.close() phải chạy TRƯỚC stop_ffmpeg (dọn dẹp thứ tự ngược)")
        self.assertLess(calls.index("stop_ffmpeg"), calls.index("stop_xvfb"))


class _Clock:
    """Fake `time` module for recorder.py — monotonic() returns a settable value, sleep() is a no-op."""

    def __init__(self, now: float = 100.0):
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def sleep(self, _s: float) -> None:
        return None


class _FakeProc:
    def __init__(self, argv, exit_rc=None, **kw):
        self.argv = list(argv)
        self.kw = kw
        self.returncode = exit_rc

    def poll(self):
        return self.returncode

    def communicate(self, input=None, timeout=None):
        return (b"", b"")

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class _PopenRecorder:
    def __init__(self):
        self.procs: list[_FakeProc] = []

    def __call__(self, argv, **kw):
        p = _FakeProc(argv, **kw)
        self.procs.append(p)
        return p


class TestStartXvfb(unittest.TestCase):
    """Regression: a busy display made OUR Xvfb exit at once,
    and ffmpeg + the browser then silently recorded the OTHER session's screen (:236)."""

    def _start(self, exit_rc):
        rec = recorder_mod.DemoRecorder(display=":203", out_path=UNUSED_MP4,
                                        profile_dir="/tmp/unused-prof", resolution="1280x720",
                                        xvfb_startup_wait=0.5)
        procs, sleeps = [], []

        def popen(argv, **kw):
            procs.append(_FakeProc(argv, exit_rc=exit_rc, **kw))
            return procs[-1]

        clock = _Clock()
        clock.sleep = sleeps.append
        with mock.patch.object(recorder_mod, "time", clock), \
                mock.patch.object(recorder_mod.subprocess, "Popen", popen):
            rec.start_xvfb()
        return rec, procs, sleeps

    def test_exited_xvfb_raises_runtime_error_naming_the_display(self):
        with self.assertRaises(RuntimeError) as cm:
            self._start(exit_rc=1)
        self.assertIn(":203", str(cm.exception))

    def test_exit_check_runs_after_the_startup_sleep(self):
        # Xvfb on a busy display exits only after a few ms: poll() read BEFORE the sleep still
        # says "running" (a mutant stayed green). This proc is alive until the
        # startup sleep has happened, then reports rc=1 → the check must still raise.
        rec = recorder_mod.DemoRecorder(display=":203", out_path=UNUSED_MP4,
                                        profile_dir="/tmp/unused-prof", xvfb_startup_wait=0.5)
        state = {"slept": False}

        class LateExit(_FakeProc):
            def poll(self):
                return 1 if state["slept"] else None

            @property
            def returncode(self):
                return 1 if state["slept"] else None

            @returncode.setter
            def returncode(self, _v):
                pass

        clock = _Clock()
        clock.sleep = lambda _s: state.update(slept=True)
        with mock.patch.object(recorder_mod, "time", clock), \
                mock.patch.object(recorder_mod.subprocess, "Popen", lambda argv, **kw: LateExit(argv, **kw)):
            with self.assertRaises(RuntimeError):
                rec.start_xvfb()
        self.assertTrue(state["slept"])

    def test_running_xvfb_is_kept_after_the_startup_wait(self):
        rec, procs, sleeps = self._start(exit_rc=None)
        self.assertIs(rec._xvfb_proc, procs[0])
        self.assertEqual(procs[0].argv[:2], ["Xvfb", ":203"])
        self.assertEqual(sleeps, [0.5], "the check runs after the startup wait")

    def test_session_does_not_start_ffmpeg_when_xvfb_exited(self):
        rec = recorder_mod.DemoRecorder(display=":203", out_path=UNUSED_MP4,
                                        profile_dir="/tmp/unused-prof", xvfb_startup_wait=0, capture="x11")
        started = []

        def popen(argv, **kw):
            started.append(argv[0])
            return _FakeProc(argv, exit_rc=1 if argv[0] == "Xvfb" else None)

        fake_async_api = types.SimpleNamespace(async_playwright=lambda: _FakePlaywrightCM(_FakeChromiumRaises()))

        async def run():
            sys.modules["playwright.async_api"] = fake_async_api
            try:
                async with rec.session():
                    self.fail("never reached")
            finally:
                del sys.modules["playwright.async_api"]

        with mock.patch.object(recorder_mod, "time", _Clock()), \
                mock.patch.object(recorder_mod.subprocess, "Popen", popen), self.assertRaises(RuntimeError):
            asyncio.run(run())
        self.assertEqual(started, ["Xvfb"], "no ffmpeg capture of somebody else's display")


class TestVideoClock(unittest.TestCase):
    def _rec(self):
        rec = recorder_mod.DemoRecorder(display=":99", out_path=UNUSED_MP4, profile_dir="/tmp/unused-prof")
        self.addCleanup(lambda: rec._ffmpeg_log_fh and rec._ffmpeg_log_fh.close())   # no stop_ffmpeg here
        return rec

    def test_start_ffmpeg_sets_video_t0_and_disables_mouse_drawing(self):
        rec = self._rec()
        clock, popen = _Clock(100.0), _PopenRecorder()
        self.assertIsNone(rec.video_t0)

        with mock.patch.object(recorder_mod, "time", clock), \
                mock.patch.object(recorder_mod.subprocess, "Popen", popen):
            rec.start_ffmpeg()

        self.assertEqual(rec.video_t0, 100.0)
        argv = popen.procs[0].argv
        self.assertEqual(argv[0], "ffmpeg")
        i = argv.index("-draw_mouse")
        self.assertEqual(argv[i + 1], "0", "the X cursor never moves under CDP — drawing it adds a stray arrow")
        self.assertEqual(argv[argv.index("-i") + 1], ":99.0")
        self.assertEqual(argv[-1], UNUSED_MP4)

    def test_draw_mouse_is_an_input_option_before_dash_i_fix_pointer_in_video(self):
        # Regression: `-draw_mouse 0` AFTER `-i` is parsed as an OUTPUT
        # option and silently ignored by ffmpeg → the X pointer (a black hand over clickable
        # rows) was captured at screen centre in every video. It must precede `-i`, and the
        # x11grab input options (-f x11grab, -video_size) belong to the same input group.
        rec = self._rec()
        popen = _PopenRecorder()
        with mock.patch.object(recorder_mod, "time", _Clock()), \
                mock.patch.object(recorder_mod.subprocess, "Popen", popen):
            rec.start_ffmpeg()
        argv = popen.procs[0].argv
        self.assertEqual(argv.count("-i"), 1)
        self.assertEqual(argv.count("-draw_mouse"), 1)
        i_in = argv.index("-i")
        i_dm = argv.index("-draw_mouse")
        self.assertLess(i_dm, i_in, f"-draw_mouse must come before -i (else ignored): {argv}")
        self.assertEqual(argv[i_dm + 1], "0")
        self.assertLess(argv.index("x11grab"), i_dm)
        self.assertEqual(argv[argv.index("-f") + 1], "x11grab")
        self.assertLess(argv.index("-video_size"), i_in)

    def test_video_t0_stays_none_when_ffmpeg_spawn_fails(self):
        rec = self._rec()

        def boom(*a, **kw):
            raise FileNotFoundError("ffmpeg")

        with mock.patch.object(recorder_mod, "time", _Clock()), \
                mock.patch.object(recorder_mod.subprocess, "Popen", boom):
            with self.assertRaises(FileNotFoundError):
                rec.start_ffmpeg()
        self.assertIsNone(rec.video_t0)

    def test_video_elapsed_and_mark_raise_before_start_ffmpeg(self):
        rec = self._rec()
        with self.assertRaises(RuntimeError):
            rec.video_elapsed()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(RuntimeError):
            rec.mark("open_login")
        self.assertEqual(out.getvalue(), "")

    def test_mark_prints_seconds_since_video_t0_two_decimals(self):
        rec = self._rec()
        clock = _Clock(100.0)
        with mock.patch.object(recorder_mod, "time", clock), \
                mock.patch.object(recorder_mod.subprocess, "Popen", _PopenRecorder()):
            rec.start_ffmpeg()
            clock.now = 112.5
            self.assertAlmostEqual(rec.video_elapsed(), 12.5)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rec.mark("open_login")
                clock.now = 103.0
                rec.mark("save_order")
        self.assertEqual(out.getvalue(), "MARK 12.50 open_login\nMARK 3.00 save_order\n")

    def test_mark_rejects_empty_or_whitespace_step_without_printing(self):
        # build_srt's MARK regex takes ONE token: "MARK 2.70 open login" would be skipped
        # silently and the subtitle for that step lost — mark() must fail at record time.
        rec = self._rec()
        clock = _Clock(100.0)
        with mock.patch.object(recorder_mod, "time", clock), \
                mock.patch.object(recorder_mod.subprocess, "Popen", _PopenRecorder()):
            rec.start_ffmpeg()
            for bad in ("", "open login", "a\tb", "step\n", " lead", "trail ", "nb sp"):
                out = io.StringIO()
                with self.subTest(step=bad), contextlib.redirect_stdout(out), self.assertRaises(ValueError):
                    rec.mark(bad)
                self.assertEqual(out.getvalue(), "")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rec.mark("open_login-2.vi")
        self.assertEqual(out.getvalue(), "MARK 0.00 open_login-2.vi\n")


class _FakeMouse:
    def __init__(self, calls):
        self._calls = calls

    async def move(self, x, y):
        self._calls.append(("mouse_move", x, y))


class _FakePage:
    def __init__(self, calls=None):
        self.mouse = _FakeMouse(calls if calls is not None else [])


class _FakeCtxOk:
    def __init__(self, calls):
        self._calls = calls
        self.pages: list = []

    async def add_init_script(self, path=None):
        self._calls.append(("init_script", Path(path).name))

    async def new_page(self):
        self._calls.append("new_page")
        return _FakePage(self._calls)

    async def close(self):
        self._calls.append("ctx_close")


class _FakeChromiumCapture:
    def __init__(self, calls):
        self._calls = calls
        self.kwargs: dict | None = None

    async def launch_persistent_context(self, **kw):
        self.kwargs = kw
        return _FakeCtxOk(self._calls)


class TestSessionLaunchArgs(unittest.TestCase):
    def test_session_launch_kwargs_and_mark_inside_session(self):
        calls: list = []
        chromium = _FakeChromiumCapture(calls)
        fake_async_api = types.SimpleNamespace(async_playwright=lambda: _FakePlaywrightCM(chromium))
        clock, popen = _Clock(50.0), _PopenRecorder()
        out = io.StringIO()

        with tempfile.TemporaryDirectory() as profile_dir:
            rec = recorder_mod.DemoRecorder(display=":42", out_path=UNUSED_MP4,
                                            profile_dir=profile_dir, resolution="1280x720",
                                            xvfb_startup_wait=0, capture="x11")

            async def run():
                sys.modules["playwright.async_api"] = fake_async_api
                try:
                    async with rec.session() as (page, cursor):
                        self.assertIsInstance(page, _FakePage)
                        # Regression: CursorState() at (0,0)
                        # while Chrome sees the real X pointer at screen centre → the first
                        # smooth_move teleported ~1040 px in one frame.
                        self.assertEqual((cursor.x, cursor.y), (640, 360),
                                         "the cursor must start at the screen centre (1280x720)")
                        # Regression: the BROWSER mouse sat at
                        # (0,0) while CursorState said centre, so the first wheel went to the
                        # navbar and the target jumped 3380 px in one scrollIntoView.
                        self.assertIn(("mouse_move", 640, 360), calls,
                                      "the browser mouse must be parked where CursorState says")
                        self.assertLess(calls.index("new_page"), calls.index(("mouse_move", 640, 360)))
                        calls.append("body")
                        clock.now = 57.25
                        rec.mark("inside")
                finally:
                    del sys.modules["playwright.async_api"]

            with mock.patch.object(recorder_mod, "time", clock), \
                    mock.patch.object(recorder_mod.subprocess, "Popen", popen), \
                    contextlib.redirect_stdout(out):
                asyncio.run(run())

        kw = chromium.kwargs
        self.assertIsNotNone(kw)
        self.assertEqual(kw["ignore_default_args"], ["--no-sandbox", "--enable-automation"])
        self.assertIn("--disable-infobars", kw["args"])
        self.assertIs(kw["headless"], False)
        # no_viewport + a window exactly the Xvfb screen at (0,0): with viewport= Playwright
        # opened 1928x1211 at (10,10) and ~97 px of the page fell outside the x11grab frame.
        self.assertIs(kw["no_viewport"], True)
        self.assertNotIn("viewport", kw)
        self.assertIn("--window-position=0,0", kw["args"])
        # +1 px each way: on Xvfb Chrome's window comes out 1 px short of --window-size, which
        # left a black 1-px right column + bottom row in every frame (measured 2026-10-21).
        self.assertIn("--window-size=1281,721", kw["args"])
        self.assertEqual([a for a in kw["args"] if a.startswith("--window-size=")], ["--window-size=1281,721"])
        self.assertIn("--disable-notifications", kw["args"])
        self.assertEqual(kw["env"]["DISPLAY"], ":42")
        self.assertEqual(out.getvalue(), "MARK 7.25 inside\n")
        self.assertEqual([p.argv[0] for p in popen.procs], ["Xvfb", "ffmpeg"])
        self.assertIn(("init_script", "cursor_overlay.js"), calls)
        self.assertEqual(calls[-1], "ctx_close")

    def _launch_args(self, resolution: str = "1600x900", **rec_kw) -> list:
        chromium = _FakeChromiumCapture([])
        fake_async_api = types.SimpleNamespace(async_playwright=lambda: _FakePlaywrightCM(chromium))
        self.start_cursor = None
        with tempfile.TemporaryDirectory() as profile_dir:
            rec = recorder_mod.DemoRecorder(display=":42", out_path=UNUSED_MP4,
                                            profile_dir=profile_dir, resolution=resolution,
                                            xvfb_startup_wait=0, capture="x11", **rec_kw)

            async def run():
                sys.modules["playwright.async_api"] = fake_async_api
                try:
                    async with rec.session() as (_page, cursor):
                        self.start_cursor = (cursor.x, cursor.y)
                finally:
                    del sys.modules["playwright.async_api"]

            with mock.patch.object(recorder_mod, "time", _Clock()), \
                    mock.patch.object(recorder_mod.subprocess, "Popen", _PopenRecorder()):
                asyncio.run(run())
        self.assertIsNotNone(chromium.kwargs)
        return chromium.kwargs["args"]

    def test_cursor_starts_at_screen_centre_for_each_resolution(self):
        # Regression: first smooth_move jumped from (0,0).
        for res, centre in (("1920x1080", (960, 540)), ("1601x899", (800.5, 449.5))):
            with self.subTest(resolution=res):
                self._launch_args(resolution=res)
                self.assertEqual(self.start_cursor, centre)

    def test_kiosk_is_default_so_no_tab_or_address_bar_in_video(self):
        # Regression: the tab strip + address bar showed in every video.
        args = self._launch_args()
        self.assertEqual(args.count("--kiosk"), 1, args)
        # Xvfb has no window manager: kiosk alone covers ~2/3 of the screen — size stays explicit
        # (+1 px: the window comes out 1 px short on Xvfb).
        self.assertIn("--window-size=1601,901", args)
        self.assertIn("--window-position=0,0", args)

    def test_kiosk_false_restores_the_normal_window(self):
        args = self._launch_args(kiosk=False)
        self.assertNotIn("--kiosk", args)
        self.assertIn("--window-size=1601,901", args)
        self.assertIn("--window-position=0,0", args)


# ---------------------------------------------------------------------------------------------
# ffmpeg log split from the
# MARK stdout, and subtitle pacing (mark_paced / cue_left / the post-yield wait of session()).

class _AsyncioSleeps:
    """Stands in for recorder.py's `asyncio` module: records every sleep, advances the fake clock."""

    def __init__(self, clock=None, log=None):
        self.clock, self.log, self.sleeps = clock, log, []

    async def sleep(self, s, *a, **kw):
        self.sleeps.append(s)
        if self.log is not None:
            self.log.append(("sleep", s))
        if self.clock is not None:
            self.clock.now += s


class _TmpOut(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rdv-rec-")
        self.addCleanup(self._tmp.cleanup)
        self.out = os.path.join(self._tmp.name, "take1.mp4")


class TestFfmpegLog(_TmpOut):
    """Regression: ffmpeg's `\\r` progress shared stdout with MARK lines."""

    def _started(self, proc_cls=None):
        rec = recorder_mod.DemoRecorder(display=":99", out_path=self.out, profile_dir="/tmp/unused-prof")
        popen = _PopenRecorder()
        if proc_cls is not None:
            def popen(argv, **kw):            # noqa: F811 — custom proc class
                p = proc_cls(argv, **kw)
                popen.procs.append(p)
                return p
            popen.procs = []
        with mock.patch.object(recorder_mod, "time", _Clock()), \
                mock.patch.object(recorder_mod.subprocess, "Popen", popen):
            rec.start_ffmpeg()
        self.addCleanup(lambda: rec._ffmpeg_log_fh and rec._ffmpeg_log_fh.close())
        return rec, popen.procs[0]

    def test_log_path_is_next_to_the_video(self):
        rec = recorder_mod.DemoRecorder(display=":99", out_path=self.out, profile_dir="/tmp/unused-prof")
        self.assertEqual(rec.ffmpeg_log, self.out + ".ffmpeg.log")

    def test_popen_gets_the_log_file_for_stdout_and_stderr_fix(self):
        rec, proc = self._started()
        fh = proc.kw.get("stderr")
        self.assertIsNotNone(fh, "ffmpeg stderr (the \\r progress) must not go to our stdout")
        self.assertIs(proc.kw.get("stdout"), fh)
        self.assertEqual(os.path.abspath(fh.name), os.path.abspath(self.out + ".ffmpeg.log"))
        self.assertFalse(fh.closed)
        self.assertIn("b", fh.mode, "binary: ffmpeg writes raw bytes")
        self.assertIs(proc.kw.get("stdin"), recorder_mod.subprocess.PIPE, "stdin stays a pipe for the 'q' stop")
        self.assertTrue(os.path.exists(self.out + ".ffmpeg.log"))

    def test_log_is_truncated_for_a_new_take(self):
        with open(self.out + ".ffmpeg.log", "wb") as f:
            f.write(b"old take progress\n")
        rec, _ = self._started()
        rec.stop_ffmpeg()
        self.assertEqual(os.path.getsize(self.out + ".ffmpeg.log"), 0)

    def test_stop_ffmpeg_closes_the_log(self):
        rec, proc = self._started()
        fh = proc.kw["stderr"]
        rec.stop_ffmpeg()
        self.assertTrue(fh.closed)
        self.assertIsNone(rec._ffmpeg_log_fh)
        rec.stop_ffmpeg()                     # idempotent: a second stop does not fail on a closed file

    def test_stop_ffmpeg_closes_the_log_on_the_timeout_path(self):
        calls = []

        class Stuck(_FakeProc):
            def communicate(self, input=None, timeout=None):
                calls.append(("communicate", input))
                raise recorder_mod.subprocess.TimeoutExpired("ffmpeg", timeout)

            def send_signal(self, sig):
                calls.append(("signal", sig))

            def wait(self, timeout=None):
                calls.append("wait")
                return 0

        rec, proc = self._started(Stuck)
        fh = proc.kw["stderr"]
        rec.stop_ffmpeg()
        self.assertEqual(calls, [("communicate", b"q"), ("signal", recorder_mod.signal.SIGINT), "wait"])
        self.assertTrue(fh.closed)

    def test_log_closed_even_when_sigint_wait_also_times_out(self):
        class Dead(_FakeProc):
            def communicate(self, input=None, timeout=None):
                raise recorder_mod.subprocess.TimeoutExpired("ffmpeg", timeout)

            def send_signal(self, sig):
                pass

            def wait(self, timeout=None):
                raise recorder_mod.subprocess.TimeoutExpired("ffmpeg", timeout)

        rec, proc = self._started(Dead)
        fh = proc.kw["stderr"]
        with self.assertRaises(recorder_mod.subprocess.TimeoutExpired):
            rec.stop_ffmpeg()
        self.assertTrue(fh.closed)

    def test_popen_failure_closes_the_log_and_reraises(self):
        # Regression: the handle opened before Popen stayed
        # open when Popen raised (ffmpeg missing) — stop_ffmpeg() returns early with no proc.
        rec = recorder_mod.DemoRecorder(display=":99", out_path=self.out, profile_dir="/tmp/unused-prof")
        opened = []
        real_open = open

        def spy_open(*a, **kw):
            fh = real_open(*a, **kw)
            opened.append(fh)
            return fh

        def boom(*a, **kw):
            raise FileNotFoundError("ffmpeg")

        with mock.patch.object(recorder_mod, "time", _Clock()), \
                mock.patch.object(recorder_mod.subprocess, "Popen", boom), \
                mock.patch("builtins.open", spy_open), self.assertRaises(FileNotFoundError):
            rec.start_ffmpeg()
        self.assertEqual(len(opened), 1, "premise: start_ffmpeg opened the log")
        self.assertTrue(opened[0].closed, "the log handle leaked after a failed spawn")
        self.assertIsNone(rec._ffmpeg_log_fh)
        self.assertIsNone(rec.video_t0)
        rec.stop_ffmpeg()                     # still a no-op afterwards

    def test_mark_goes_to_stdout_not_the_ffmpeg_log(self):
        rec, proc = self._started()
        out = io.StringIO()
        with mock.patch.object(recorder_mod, "time", _Clock(100.0)), contextlib.redirect_stdout(out):
            rec.mark("open_form")
        rec.stop_ffmpeg()
        self.assertEqual(out.getvalue(), "MARK 0.00 open_form\n")
        with open(self.out + ".ffmpeg.log", "rb") as f:
            self.assertEqual(f.read(), b"")


class TestReusedRecorderPerTakeState(_TmpOut):
    """Regression: one recorder, two session() takes. Take 1's
    last mark (on take 1's clock) froze take 2's first mark_paced for ~the length of take 1, and the
    ffmpeg log path stayed the one derived from __init__'s out_path."""

    def _take(self, rec, clock, popen):
        with mock.patch.object(recorder_mod, "time", clock), \
                mock.patch.object(recorder_mod.subprocess, "Popen", popen):
            rec.start_ffmpeg()

    def test_second_take_does_not_inherit_the_last_mark(self):
        clock, aio = _Clock(100.0), None
        aio = _AsyncioSleeps(clock)
        rec = recorder_mod.DemoRecorder(display=":99", out_path=self.out, profile_dir="/tmp/unused-prof")
        rec.pace_from(NARR)
        with mock.patch.object(recorder_mod, "asyncio", aio), contextlib.redirect_stdout(io.StringIO()):
            self._take(rec, clock, _PopenRecorder())
            clock.now = 200.0
            with mock.patch.object(recorder_mod, "time", clock):
                rec.mark("open_form")                     # take 1: elapsed 100, needs 5 s
                rec.stop_ffmpeg()
            clock.now = 300.0
            self._take(rec, clock, _PopenRecorder())      # take 2 starts: elapsed 0
            with mock.patch.object(recorder_mod, "time", clock):
                self.assertIsNone(rec._last_mark)
                self.assertEqual(rec.cue_left(), 0.0)
                asyncio.run(rec.mark_paced("save"))
                rec.stop_ffmpeg()
        self.assertEqual(aio.sleeps, [0.0], "take 2's first mark must not wait out take 1's clock")

    def test_ffmpeg_log_follows_out_path_changed_between_takes(self):
        rec = recorder_mod.DemoRecorder(display=":99", out_path=self.out, profile_dir="/tmp/unused-prof")
        p1 = _PopenRecorder()
        self._take(rec, _Clock(), p1)
        p1.procs[0].kw["stderr"].write(b"take 1 progress\n")
        rec.stop_ffmpeg()
        take2 = os.path.join(self._tmp.name, "take2.mp4")
        rec.out_path = take2
        p2 = _PopenRecorder()
        self._take(rec, _Clock(), p2)
        self.addCleanup(lambda: rec._ffmpeg_log_fh and rec._ffmpeg_log_fh.close())
        self.assertEqual(rec.ffmpeg_log, take2 + ".ffmpeg.log")
        self.assertEqual(p2.procs[0].kw["stderr"].name, take2 + ".ffmpeg.log")
        self.assertEqual(p2.procs[0].argv[-1], take2)
        with open(self.out + ".ffmpeg.log", "rb") as f:
            self.assertEqual(f.read(), b"take 1 progress\n", "take 1's log must not be overwritten")


NARR = {
    "open_form": {"vi": "Mở phiếu", "ko": "x" * 80, "en": "Open the form"},    # ko: 80/16 = 5 s
    "save": {"vi": "Lưu"},
    "silent": {"vi": "", "ko": ""},
    "mixed": {"vi": "", "en": "y" * 64},                                         # 4 s
}


class TestPacing(unittest.TestCase):
    """cue_left()/mark_paced(): the step just marked stays on screen long enough to read its
    LONGEST narration (one take serves every subtitle language) — cues lasted 2-4 s in a real take."""

    def setUp(self):
        self.clock = _Clock(100.0)
        self.aio = _AsyncioSleeps(self.clock)
        for target, new in (("time", self.clock), ("asyncio", self.aio)):
            p = mock.patch.object(recorder_mod, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.rec = recorder_mod.DemoRecorder(display=":99", out_path=UNUSED_MP4, profile_dir="/tmp/unused-prof")
        self.rec.video_t0 = 100.0             # as start_ffmpeg would set it (no real Popen / log file)
        self.out = io.StringIO()
        r = contextlib.redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def at(self, elapsed):
        self.clock.now = 100.0 + elapsed

    def test_zero_when_pace_from_never_called(self):
        self.at(10)
        self.rec.mark("open_form")
        self.at(11)
        self.assertEqual(self.rec.cue_left(), 0.0)

    def test_zero_before_any_mark(self):
        self.rec.pace_from(NARR)
        self.at(5)
        self.assertEqual(self.rec.cue_left(), 0.0)

    def test_waits_for_the_longest_language(self):
        self.rec.pace_from(NARR)
        self.at(10)
        self.rec.mark("open_form")
        self.at(11)
        self.assertAlmostEqual(self.rec.cue_left(), 4.0)   # 10 + max(3, 5, 3) - 11

    def test_language_with_empty_text_is_ignored_not_zeroing_the_wait(self):
        self.rec.pace_from(NARR)
        self.at(10)
        self.rec.mark("mixed")
        self.at(10.5)
        self.assertAlmostEqual(self.rec.cue_left(), 3.5)   # only en counts: 64/16 = 4

    def test_short_text_uses_min_cue(self):
        self.rec.pace_from(NARR)
        self.at(2)
        self.rec.mark("save")
        self.at(3)
        self.assertAlmostEqual(self.rec.cue_left(), 2.0)

    def test_zero_for_unknown_step_and_for_a_step_without_text(self):
        self.rec.pace_from(NARR)
        for step in ("not_in_narration", "silent"):
            with self.subTest(step=step):
                self.at(10)
                self.rec.mark(step)
                self.at(10.1)
                self.assertEqual(self.rec.cue_left(), 0.0)

    def test_never_negative_once_the_cue_had_its_time(self):
        self.rec.pace_from(NARR)
        self.at(10)
        self.rec.mark("open_form")
        for later in (15.0, 15.001, 40.0):
            with self.subTest(at=later):
                self.at(later)
                self.assertEqual(self.rec.cue_left(), 0.0)

    def test_custom_min_cue_and_cps(self):
        self.rec.pace_from({"a": {"vi": "z" * 20}}, min_cue=1.0, cps=10.0)
        self.at(0)
        self.rec.mark("a")
        self.assertAlmostEqual(self.rec.cue_left(), 2.0)
        self.rec.pace_from({"a": {"vi": "z"}}, min_cue=1.0, cps=10.0)
        self.assertAlmostEqual(self.rec.cue_left(), 1.0)

    def test_mark_paced_sleeps_the_previous_cue_then_marks(self):
        self.rec.pace_from(NARR)
        self.at(10)
        self.rec.mark("open_form")
        self.at(11)
        asyncio.run(self.rec.mark_paced("save"))
        self.assertEqual(len(self.aio.sleeps), 1)
        self.assertAlmostEqual(self.aio.sleeps[0], 4.0)
        self.assertEqual(self.out.getvalue(), "MARK 10.00 open_form\nMARK 15.00 save\n")
        self.assertEqual(self.rec._last_mark, (15.0, "save"))

    def test_mark_paced_without_pace_from_raises_and_prints_nothing(self):
        # silently behaving like mark() recorded a whole take with
        # 2-4 s cues, only noticed after burning the subtitles.
        self.at(3)
        with self.assertRaises(RuntimeError) as cm:
            asyncio.run(self.rec.mark_paced("open_form"))
        self.assertIn("pace_from", str(cm.exception))
        self.assertEqual(self.aio.sleeps, [], "no wait before the error")
        self.assertEqual(self.out.getvalue(), "")
        self.assertIsNone(self.rec._last_mark)

    def test_first_mark_paced_after_pace_from_does_not_wait(self):
        self.rec.pace_from(NARR)
        self.at(3)
        asyncio.run(self.rec.mark_paced("open_form"))             # first mark of the take
        self.assertEqual(self.aio.sleeps, [0.0])
        self.assertEqual(self.out.getvalue(), "MARK 3.00 open_form\n")

    def test_mark_paced_validates_the_step_like_mark(self):
        self.rec.pace_from(NARR)
        with self.assertRaises(ValueError):
            asyncio.run(self.rec.mark_paced("open form"))

    def test_mark_unchanged_prints_immediately_without_sleeping(self):
        self.rec.pace_from(NARR)
        self.at(10)
        self.rec.mark("open_form")
        self.at(10.5)
        self.rec.mark("save")
        self.assertEqual(self.aio.sleeps, [])
        self.assertEqual(self.out.getvalue(), "MARK 10.00 open_form\nMARK 10.50 save\n")


class TestSessionWaitsOutTheLastCue(unittest.TestCase):
    """session() keeps recording after the body until the LAST marked step had its reading time —
    on a normal exit only (an exception must not hold a broken take open)."""

    def _session(self, body, pace=True):
        calls: list = []
        clock = _Clock(50.0)
        aio = _AsyncioSleeps(clock, calls)
        chromium = _FakeChromiumCapture(calls)
        fake_async_api = types.SimpleNamespace(async_playwright=lambda: _FakePlaywrightCM(chromium))
        rec = recorder_mod.DemoRecorder(display=":42", out_path=UNUSED_MP4,
                                        profile_dir="/tmp/unused-prof", resolution="1280x720", capture="x11")

        def start_ffmpeg():
            calls.append("start_ffmpeg")
            rec.video_t0 = clock.now

        rec.start_xvfb = lambda: calls.append("start_xvfb")
        rec.start_ffmpeg = start_ffmpeg
        rec.stop_ffmpeg = lambda: calls.append("stop_ffmpeg")
        rec.stop_xvfb = lambda: calls.append("stop_xvfb")
        if pace:
            rec.pace_from(NARR)

        async def run():
            sys.modules["playwright.async_api"] = fake_async_api
            try:
                async with rec.session():
                    calls.append("body")
                    body(rec, clock)
            finally:
                del sys.modules["playwright.async_api"]

        err = None
        with mock.patch.object(recorder_mod, "time", clock), mock.patch.object(recorder_mod, "asyncio", aio), \
                mock.patch.object(Path, "mkdir"), contextlib.redirect_stdout(io.StringIO()):
            try:
                asyncio.run(run())
            except Exception as e:  # noqa: BLE001 — the test inspects it
                err = e
        return calls, aio.sleeps, err

    @staticmethod
    def _mark_last(rec, clock):
        clock.now += 10.0
        rec.mark("open_form")                 # needs 5 s (ko)
        clock.now += 1.0

    def test_normal_exit_waits_the_last_cue_before_closing(self):
        calls, sleeps, err = self._session(self._mark_last)
        self.assertIsNone(err)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 4.0)
        i = calls.index("body")
        self.assertEqual(calls[i + 1][0], "sleep", calls)
        self.assertEqual(calls[i + 2:], ["ctx_close", "stop_ffmpeg", "stop_xvfb"],
                         "the wait must happen while ffmpeg still records")

    def test_normal_exit_without_pacing_waits_zero(self):
        calls, sleeps, err = self._session(self._mark_last, pace=False)
        self.assertIsNone(err)
        self.assertEqual(sleeps, [0.0])

    def test_exception_in_body_does_not_wait_and_still_cleans_up(self):
        def boom(rec, clock):
            self._mark_last(rec, clock)
            raise ValueError("demo step failed")

        calls, sleeps, err = self._session(boom)
        self.assertIsInstance(err, ValueError)
        self.assertEqual(sleeps, [], "a failing take must not be held open for the cue")
        self.assertEqual(calls[-3:], ["ctx_close", "stop_ffmpeg", "stop_xvfb"])


if __name__ == "__main__":
    unittest.main()
