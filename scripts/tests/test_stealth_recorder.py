#!/usr/bin/env python3
"""Unit tests for stealth_recorder.py — system python3, no Xvfb / ffmpeg / Firefox.

A fake `invisible_playwright.async_api` module is injected into sys.modules (the real package
lives only in the plugin env `uvx --from invisible-playwright-mcp python`, and stealth_recorder
imports it lazily inside session()). Xvfb / ffmpeg start/stop are replaced per instance by
call recorders, like test_recorder.py does. The real stack is covered by
test_stealth_x11grab_real.py.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_stealth_recorder.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import stealth_recorder as sr  # noqa: E402


class _FakeMouse:
    def __init__(self, log):
        self.log = log

    async def move(self, x, y):
        self.log.append(("mouse_move", x, y))


class _FakePage:
    def __init__(self, log):
        self.mouse = _FakeMouse(log)


class _FakeCtx:
    def __init__(self, log):
        self.log = log

    async def new_page(self):
        self.log.append("new_page")
        return _FakePage(self.log)

    async def close(self):
        self.log.append("ctx_close")


class _FakeBrowser:
    def __init__(self, log, ctx_kwargs):
        self.log, self.ctx_kwargs = log, ctx_kwargs

    async def new_context(self, **kw):
        self.ctx_kwargs.update(kw)
        self.log.append("new_context")
        return _FakeCtx(self.log)


def fake_invisible(log, launch_kwargs, ctx_kwargs, launch_error=None):
    class InvisiblePlaywright:
        def __init__(self, *args, **kw):
            launch_kwargs.update(kw)
            launch_kwargs["_args"] = args
            launch_kwargs["_display_at_launch"] = os.environ.get("DISPLAY")

        async def __aenter__(self):
            if launch_error:
                raise launch_error
            log.append("browser_enter")
            return _FakeBrowser(log, ctx_kwargs)

        async def __aexit__(self, *exc):
            log.append("browser_exit")
            return False

    pkg = types.ModuleType("invisible_playwright")
    api = types.ModuleType("invisible_playwright.async_api")
    api.InvisiblePlaywright = InvisiblePlaywright
    pkg.async_api = api
    return {"invisible_playwright": pkg, "invisible_playwright.async_api": api}


class SessionCase(unittest.TestCase):
    def make(self, body=None, launch_error=None, start_xvfb_error=None, **rec_kw):
        """Run one StealthRecorder.session(); returns (log, launch kwargs, ctx kwargs, yielded)."""
        log, launch, ctxkw, got = [], {}, {}, {}
        rec = sr.StealthRecorder(display=":201", out_path="/tmp/unused.mp4", xvfb_startup_wait=0, **rec_kw)

        def start_xvfb():
            log.append("start_xvfb")
            if start_xvfb_error:
                raise start_xvfb_error

        rec.start_xvfb = start_xvfb
        rec.start_ffmpeg = lambda: log.append("start_ffmpeg")
        rec.stop_ffmpeg = lambda: log.append("stop_ffmpeg")
        rec.stop_xvfb = lambda: log.append("stop_xvfb")

        async def scene():
            async with rec.session() as (page, cursor):
                got.update(page=page, cursor=cursor, display=os.environ.get("DISPLAY"))
                log.append("body")
                if body:
                    body()

        with mock.patch.dict(sys.modules, fake_invisible(log, launch, ctxkw, launch_error)):
            asyncio.run(scene())
        return log, launch, ctxkw, got


class TestStealthSession(SessionCase):
    def test_launch_kwargs_kiosk_default(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            _log, launch, ctxkw, _got = self.make(resolution="1280x720")
        self.assertEqual(launch["extra_args"], ["--width=1280", "--height=720", "--kiosk"])
        self.assertIs(launch["headless"], False)
        self.assertIs(launch["show_cursor"], True, "the library draws the arrow + click ripple")
        self.assertEqual(launch["extra_prefs"], {"signon.rememberSignons": False})
        self.assertEqual(launch["seed"], 7)
        self.assertEqual(launch["locale"], "en-US")
        self.assertEqual(launch["_display_at_launch"], ":201", "Firefox must open on OUR Xvfb")
        self.assertEqual(ctxkw, {"viewport": {"width": 1280, "height": 720},
                                 "screen": {"width": 1280, "height": 720}})

    def test_kiosk_false_keeps_width_height_drops_kiosk(self):
        _log, launch, _c, _g = self.make(resolution="1600x900", kiosk=False, seed=3, locale="vi-VN")
        self.assertEqual(launch["extra_args"], ["--width=1600", "--height=900"])
        self.assertEqual((launch["seed"], launch["locale"]), (3, "vi-VN"))

    def test_yielded_cursor_is_screen_centre_and_native(self):
        log, _l, _c, got = self.make(resolution="1920x1080")
        cur = got["cursor"]
        self.assertEqual((cur.x, cur.y), (960, 540))
        self.assertIs(cur.native_motion, True)
        self.assertIsInstance(got["page"], _FakePage)
        self.assertIn(("mouse_move", 960, 540), log)

    def test_display_set_inside_and_restored_after(self):
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            _log, _l, _c, got = self.make()
            self.assertEqual(os.environ.get("DISPLAY"), ":0")
        self.assertEqual(got["display"], ":201")

    def test_display_removed_after_when_it_was_unset(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISPLAY", None)
            self.make()
            self.assertNotIn("DISPLAY", os.environ)

    def test_order_on_normal_exit_mouse_parked_at_centre_before_yield(self):
        # Regression: the browser mouse sat at (0,0) while
        # CursorState said centre → the first wheel scrolled the navbar, then a 3380 px jump.
        log, *_ = self.make(resolution="1280x720")
        self.assertEqual(log, ["start_xvfb", "start_ffmpeg", "browser_enter", "new_context", "new_page",
                               ("mouse_move", 640, 360), "body",
                               "ctx_close", "browser_exit", "stop_ffmpeg", "stop_xvfb"])

    def test_exception_in_body_cleans_up_in_reverse_and_restores_display(self):
        def boom():
            raise ValueError("demo step failed")

        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            with self.assertRaises(ValueError):
                self.make(body=boom)
            self.assertEqual(os.environ.get("DISPLAY"), ":0")

    def test_exception_in_body_cleanup_order(self):
        def boom():
            raise ValueError("demo step failed")

        log, launch, ctxkw = [], {}, {}
        rec = sr.StealthRecorder(display=":201", out_path="/tmp/unused.mp4", xvfb_startup_wait=0)
        rec.start_xvfb = lambda: log.append("start_xvfb")
        rec.start_ffmpeg = lambda: log.append("start_ffmpeg")
        rec.stop_ffmpeg = lambda: log.append("stop_ffmpeg")
        rec.stop_xvfb = lambda: log.append("stop_xvfb")

        async def scene():
            async with rec.session():
                boom()

        with mock.patch.dict(sys.modules, fake_invisible(log, launch, ctxkw)), self.assertRaises(ValueError):
            asyncio.run(scene())
        self.assertEqual(log[-4:],["ctx_close", "browser_exit", "stop_ffmpeg", "stop_xvfb"])

    def test_launch_failure_stops_ffmpeg_and_xvfb_and_restores_display(self):
        log, launch, ctxkw = [], {}, {}
        rec = sr.StealthRecorder(display=":201", out_path="/tmp/unused.mp4", xvfb_startup_wait=0)
        rec.start_xvfb = lambda: log.append("start_xvfb")
        rec.start_ffmpeg = lambda: log.append("start_ffmpeg")
        rec.stop_ffmpeg = lambda: log.append("stop_ffmpeg")
        rec.stop_xvfb = lambda: log.append("stop_xvfb")

        async def scene():
            async with rec.session():
                self.fail("never reached — the launch raises")

        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}), \
                mock.patch.dict(sys.modules, fake_invisible(log, launch, ctxkw, RuntimeError("firefox crash"))):
            with self.assertRaises(RuntimeError):
                asyncio.run(scene())
            self.assertEqual(os.environ.get("DISPLAY"), ":0")
        self.assertEqual(log, ["start_xvfb", "start_ffmpeg", "stop_ffmpeg", "stop_xvfb"])

    def test_xvfb_start_failure_restores_display(self):
        # session() sets DISPLAY before start_xvfb(); a failing Xvfb must not leave the
        # process pointing at a display that does not exist.
        with mock.patch.dict(os.environ, {"DISPLAY": ":0"}):
            with self.assertRaises(FileNotFoundError):
                self.make(start_xvfb_error=FileNotFoundError("Xvfb"))
            self.assertEqual(os.environ.get("DISPLAY"), ":0")

    def test_capture_is_x11_on_every_platform_its_session_only_has_the_xvfb_path(self):
        # Regression: on darwin the inherited capture="auto" made a StealthRecorder report "video".
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                rec = sr.StealthRecorder(display=":201", out_path="/tmp/unused.mp4")
            self.assertEqual(rec.capture, "x11")

    def test_patch_chrome_prefs_is_a_noop(self):
        rec = sr.StealthRecorder(display=":201", out_path="/tmp/unused.mp4")
        with mock.patch.object(sr.DemoRecorder, "patch_chrome_prefs") as parent:
            self.assertIsNone(rec.patch_chrome_prefs("vi"))
        parent.assert_not_called()

    def test_inherits_ffmpeg_argv_draw_mouse_before_input(self):
        # Own temp out_path: start_ffmpeg() opens `<out_path>.ffmpeg.log` for real (was /tmp/o.mp4.ffmpeg.log).
        tmp = tempfile.TemporaryDirectory(prefix="rdv-test-stealth-")
        self.addCleanup(tmp.cleanup)
        out = os.path.join(tmp.name, "o.mp4")
        rec = sr.StealthRecorder(display=":201", out_path=out, resolution="1280x720")
        popen = mock.MagicMock()
        with mock.patch.object(sys.modules["recorder"].subprocess, "Popen", popen):
            rec.start_ffmpeg()
        rec._ffmpeg_log_fh.close()
        self.assertEqual(rec.ffmpeg_log, out + ".ffmpeg.log")
        argv = popen.call_args[0][0]
        self.assertLess(argv.index("-draw_mouse"), argv.index("-i"))
        self.assertEqual(argv[argv.index("-i") + 1], ":201.0")


class _Clock:
    def __init__(self, now):
        self.now = now

    def monotonic(self):
        return self.now


class TestStealthSessionWaitsOutTheLastCue(unittest.TestCase):
    """StealthRecorder.session() has its OWN body (it does not call the parent's
    session()), so the post-yield wait for the last cue must be there too — normal exit only."""

    def _session(self, body, pace=True):
        log, launch, ctxkw = [], {}, {}
        clock = _Clock(50.0)
        sleeps = []

        async def fake_sleep(s, *a, **kw):
            sleeps.append(s)
            log.append(("sleep", s))
            clock.now += s

        rec = sr.StealthRecorder(display=":201", out_path="/tmp/unused.mp4", xvfb_startup_wait=0)

        def start_ffmpeg():
            log.append("start_ffmpeg")
            rec.video_t0 = clock.now

        rec.start_xvfb = lambda: log.append("start_xvfb")
        rec.start_ffmpeg = start_ffmpeg
        rec.stop_ffmpeg = lambda: log.append("stop_ffmpeg")
        rec.stop_xvfb = lambda: log.append("stop_xvfb")
        if pace:
            rec.pace_from({"open_form": {"vi": "Mở phiếu", "ko": "x" * 80}})   # ko: 5 s

        async def scene():
            async with rec.session():
                log.append("body")
                clock.now += 10.0
                rec.mark("open_form")
                clock.now += 1.0
                body()

        err = None
        # `recorder` = the module stealth_recorder imported (its `time` drives video_elapsed()).
        with mock.patch.dict(sys.modules, fake_invisible(log, launch, ctxkw)), \
                mock.patch.object(sys.modules["recorder"], "time", clock), \
                mock.patch.object(sr, "asyncio", types.SimpleNamespace(sleep=fake_sleep)), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                asyncio.run(scene())
            except Exception as e:  # noqa: BLE001 — the test inspects it
                err = e
        return log, sleeps, err

    def test_normal_exit_waits_the_last_cue_before_closing(self):
        log, sleeps, err = self._session(lambda: None)
        self.assertIsNone(err)
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 4.0)                 # 10 + 5 - 11
        i = log.index("body")
        self.assertEqual(log[i + 1][0], "sleep", log)
        self.assertEqual(log[i + 2:], ["ctx_close", "browser_exit", "stop_ffmpeg", "stop_xvfb"],
                         "the wait must happen while ffmpeg still records")

    def test_normal_exit_without_pacing_waits_zero(self):
        _log, sleeps, err = self._session(lambda: None, pace=False)
        self.assertIsNone(err)
        self.assertEqual(sleeps, [0.0])

    def test_exception_in_body_does_not_wait_and_still_cleans_up(self):
        def boom():
            raise ValueError("demo step failed")

        log, sleeps, err = self._session(boom)
        self.assertIsInstance(err, ValueError)
        self.assertEqual(sleeps, [], "a failing take must not be held open for the cue")
        self.assertEqual(log[-4:], ["ctx_close", "browser_exit", "stop_ffmpeg", "stop_xvfb"])

    def test_inherits_pacing_api(self):
        rec = sr.StealthRecorder(display=":201", out_path="/tmp/unused.mp4")
        self.assertEqual(rec.ffmpeg_log, "/tmp/unused.mp4.ffmpeg.log")
        self.assertEqual(rec.cue_left(), 0.0)


class TestMakeRecorder(unittest.TestCase):
    def mk(self, available, driver="auto", **kw):
        # These cases describe the Linux (Xvfb + x11grab) behaviour; on darwin make_recorder always picks the
        # Chrome DemoRecorder, so pin the platform. The darwin behaviour is in test_recorder_video_capture.py.
        with mock.patch.object(sr, "stealth_available", lambda: available), \
                mock.patch.object(sys, "platform", "linux"):
            return sr.make_recorder(":202", "/tmp/o.mp4", "/tmp/prof-x", driver=driver, **kw)

    def test_auto_picks_stealth_when_available(self):
        rec = self.mk(True, resolution="1280x720")
        self.assertIs(type(rec), sr.StealthRecorder)
        self.assertEqual((rec.display, rec.out_path, rec.resolution), (":202", "/tmp/o.mp4", "1280x720"))

    def test_auto_falls_back_to_chrome_with_one_stderr_line_naming_uvx(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rec = self.mk(False, resolution="1280x720")
        self.assertIs(type(rec), sr.DemoRecorder)
        self.assertEqual(rec.profile_dir, "/tmp/prof-x")
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("uvx --from invisible-playwright-mcp python", lines[0])

    def test_no_stderr_line_when_stealth_or_forced_chrome(self):
        for avail, driver in ((True, "auto"), (True, "chrome"), (False, "chrome")):
            err = io.StringIO()
            with self.subTest(available=avail, driver=driver), contextlib.redirect_stderr(err):
                self.mk(avail, driver=driver)
            self.assertEqual(err.getvalue(), "")

    def test_kw_unknown_to_both_drivers_raises_type_error_naming_it(self):
        # Regression: typos (`kisok=`, `resoluton=`, `sed=`) were dropped silently
        # and the take ran with defaults — e.g. WITH kiosk after asking for none.
        for avail, driver in ((True, "auto"), (True, "stealth"), (False, "auto"), (True, "chrome"), (False, "chrome")):
            for typo in ("kisok", "resoluton", "sed"):
                with self.subTest(available=avail, driver=driver, typo=typo), \
                        contextlib.redirect_stderr(io.StringIO()), self.assertRaises(TypeError) as cm:
                    self.mk(avail, driver=driver, resolution="1280x720", **{typo: 1})
                self.assertIn(typo, str(cm.exception))

    def test_typo_raises_even_next_to_valid_other_driver_kw(self):
        with self.assertRaises(TypeError) as cm:
            self.mk(True, chrome_binary="/opt/chrome", kisok=False)
        self.assertIn("kisok", str(cm.exception))
        self.assertNotIn("chrome_binary", str(cm.exception))

    def test_kw_of_the_other_driver_are_dropped(self):
        # One demo script, both drivers: chrome-only kw (chrome_binary) must not break stealth,
        # stealth-only kw (seed, locale) must not break the Chrome fallback.
        kw = dict(resolution="1280x720", chrome_binary="/opt/chrome", seed=11, locale="vi-VN", kiosk=False)
        st = self.mk(True, **kw)
        self.assertIs(type(st), sr.StealthRecorder)
        self.assertEqual((st.seed, st.locale, st.kiosk, st.resolution), (11, "vi-VN", False, "1280x720"))
        with contextlib.redirect_stderr(io.StringIO()):
            cr = self.mk(False, **kw)
        self.assertIs(type(cr), sr.DemoRecorder)
        self.assertEqual((cr.chrome_binary, cr.kiosk, cr.resolution), ("/opt/chrome", False, "1280x720"))
        self.assertFalse(hasattr(cr, "seed"))

    def test_forced_stealth_missing_raises_with_the_uvx_command(self):
        with self.assertRaises(RuntimeError) as cm:
            self.mk(False, driver="stealth")
        self.assertIn("uvx --from invisible-playwright-mcp python", str(cm.exception))

    def test_forced_stealth_available(self):
        self.assertIs(type(self.mk(True, driver="stealth")), sr.StealthRecorder)

    def test_forced_chrome_even_when_stealth_available(self):
        self.assertIs(type(self.mk(True, driver="chrome")), sr.DemoRecorder)

    def test_bad_driver_raises_value_error(self):
        for avail in (True, False):
            with self.subTest(available=avail), self.assertRaises(ValueError):
                self.mk(avail, driver="firefox")


class TestStealthAvailable(unittest.TestCase):
    def test_reports_find_spec_of_invisible_playwright(self):
        for spec, want in ((object(), True), (None, False)):
            with self.subTest(want=want), \
                    mock.patch.object(sr.importlib.util, "find_spec", return_value=spec) as fs:
                self.assertIs(sr.stealth_available(), want)
            fs.assert_called_with("invisible_playwright")

    def test_module_imports_without_the_package(self):
        # Under system python3 the package is absent, yet stealth_recorder imported above.
        self.assertTrue(hasattr(sr, "make_recorder"))


if __name__ == "__main__":
    unittest.main()
