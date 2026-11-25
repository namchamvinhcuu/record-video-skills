#!/usr/bin/env python3
"""Tests for the macOS capture path of recorder.py (`capture="video"`: Playwright record_video_dir + an
ffmpeg webm -> mp4 convert) and the darwin rules of stealth_recorder.make_recorder.

Hermetic like test_recorder.py: the real playwright is never importable (a fake `playwright.async_api` is
injected per test), `subprocess.run` / `shutil.which` are patched (no real ffmpeg, no Chromium), `time` is a
fake clock. Every test owns a mkdtemp root and removes it. The platform is always pinned with
`mock.patch.object(sys, "platform", ...)`, so the file gives the same result on darwin and Linux.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_recorder_video_capture.py
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.machinery
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
sys.modules["playwright"] = None          # the real playwright must never be importable here (see test_recorder.py)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

_spec = importlib.util.spec_from_file_location("recorder_video_mod", SCRIPTS_DIR / "recorder.py")
assert _spec and _spec.loader
recorder_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = recorder_mod
_spec.loader.exec_module(recorder_mod)

import stealth_recorder as sr  # noqa: E402

FFMPEG = "/fake/bin/ffmpeg"
LAUNCH_AT = 200.0
NARR = {"open_form": {"vi": "Mở phiếu", "ko": "x" * 80}}      # ko: 80 chars / 16 cps = 5 s


# ---------------------------------------------------------------------------------------------
# capture resolution

class TestCaptureResolution(unittest.TestCase):
    def _capture(self, platform, **kw):
        with mock.patch.object(sys, "platform", platform):
            return recorder_mod.DemoRecorder(display=":99", out_path="/tmp/unused.mp4",
                                             profile_dir="/tmp/unused-prof", **kw).capture

    def test_auto_is_video_on_darwin_and_x11_elsewhere(self):
        # win32 -> video is a deliberate edit of the plugin copy (Windows has no Xvfb/x11grab).
        for platform, want in (("darwin", "video"), ("linux", "x11"), ("win32", "video"), ("freebsd14", "x11")):
            with self.subTest(platform=platform):
                self.assertEqual(self._capture(platform, capture="auto"), want)

    def test_default_argument_is_auto(self):
        self.assertEqual(self._capture("darwin"), "video")
        self.assertEqual(self._capture("linux"), "x11")

    def test_explicit_value_wins_over_the_platform(self):
        for platform in ("darwin", "linux"):
            for value in ("x11", "video"):
                with self.subTest(platform=platform, capture=value):
                    self.assertEqual(self._capture(platform, capture=value), value)

    def test_bad_value_raises_value_error_naming_it(self):
        for bad in ("", "X11", "grab", "auto ", None, 1):
            with self.subTest(capture=bad), self.assertRaises(ValueError) as cm:
                self._capture("linux", capture=bad)
            self.assertIn(repr(bad), str(cm.exception))

    def test_video_mode_session_never_starts_xvfb_or_x11grab(self):
        env = _Env(self)
        called = []
        env.rec.start_xvfb = lambda: called.append("start_xvfb")
        env.rec.start_ffmpeg = lambda: called.append("start_ffmpeg")
        popen = mock.Mock(side_effect=AssertionError("capture=video must not Popen anything"))
        with mock.patch.object(recorder_mod.subprocess, "Popen", popen):
            res = env.run()
        self.assertIsNone(res.err)
        self.assertEqual(called, [])
        popen.assert_not_called()


# ---------------------------------------------------------------------------------------------
# fake playwright + test scaffolding

class _FakeMouse:
    def __init__(self, events):
        self._events = events

    async def move(self, x, y):
        self._events.append(("mouse_move", x, y))


class _FakeVideo:
    def __init__(self, ctx, webm, events):
        self._ctx, self._webm, self._events = ctx, webm, events

    async def path(self):
        # Playwright finalises the webm on ctx.close(); reading the path earlier is a bug in the caller.
        if not self._ctx.closed:
            raise AssertionError("page.video.path() read before ctx.close()")
        self._events.append("video_path")
        return self._webm


class _FakePage:
    def __init__(self, ctx, events, webm, with_video=True):
        self.mouse = _FakeMouse(events)
        self.video = _FakeVideo(ctx, webm, events) if with_video else None


class _FakeCtx:
    def __init__(self, events, webm, init_fail, new_page_fail):
        self._events, self._webm = events, webm
        self._init_fail, self._new_page_fail = init_fail, new_page_fail
        self.pages: list = []
        self.closed = False

    async def add_init_script(self, path=None):
        if self._init_fail:
            raise self._init_fail
        self._events.append(("init_script", Path(path).name))

    async def new_page(self):
        if self._new_page_fail:
            raise self._new_page_fail
        self._events.append("new_page")
        page = _FakePage(self, self._events, self._webm)
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True
        self._events.append("ctx_close")
        Path(self._webm).write_bytes(b"fake webm")   # Playwright finalises the webm inside record_video_dir on close


class _FakeChromium:
    def __init__(self, events, clock, init_fail, new_page_fail):
        self._a = (events, init_fail, new_page_fail)
        self._events, self._clock = events, clock
        self.kwargs: dict | None = None
        self.calls = 0
        self.webm: str | None = None
        self.ctx: _FakeCtx | None = None
        self.on_launch = None

    async def launch_persistent_context(self, **kw):
        self.calls += 1
        self.kwargs = kw
        self._events.append("launch")
        if self.on_launch:
            self.on_launch(kw)
        self._clock.now = LAUNCH_AT           # the launch "takes" 100 s: video_t0 must be taken AFTER it
        events, init_fail, new_page_fail = self._a
        self.webm = os.path.join(kw["record_video_dir"], "page-video.webm")
        self.ctx = _FakeCtx(events, self.webm, init_fail, new_page_fail)
        return self.ctx


class _PlaywrightCM:
    def __init__(self, chromium):
        self.chromium = chromium

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Clock:
    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now


class _AsyncioSleeps:
    """Stands in for recorder.py's `asyncio`: records every sleep, advances the fake clock."""

    def __init__(self, clock, events):
        self.clock, self.events, self.sleeps = clock, events, []

    async def sleep(self, s, *a, **kw):
        self.sleeps.append(s)
        self.events.append(("sleep", s))
        self.clock.now += s


class _Env:
    """One DemoRecorder(capture="video") plus every patch needed to run its session() hermetically."""

    def __init__(self, case, which=FFMPEG, rc=0, run_exc=None, init_fail=None, new_page_fail=None,
                 chrome_binary=None, resolution="1280x720", pace=False, fake_asyncio=False,
                 out_name="take.mp4", no_playwright=False):
        self.case = case
        self.root = tempfile.mkdtemp(prefix="rdv-test-vcap-")      # also holds every temp video dir (see mkdtemp)
        case.addCleanup(shutil.rmtree, self.root, True)
        self.which, self.rc, self.run_exc = which, rc, run_exc
        self.no_playwright = no_playwright
        self.events: list = []
        self.clock = _Clock(100.0)
        self.chromium = _FakeChromium(self.events, self.clock, init_fail, new_page_fail)
        self.aio = _AsyncioSleeps(self.clock, self.events) if fake_asyncio else None
        self.out_path = os.path.join(self.root, out_name)
        self.profile = os.path.join(self.root, "profile")
        self.made_dirs: list[str] = []
        self.run_calls: list[tuple] = []
        with mock.patch.object(sys, "platform", "darwin"):
            self.rec = recorder_mod.DemoRecorder(
                display=":99", out_path=self.out_path, profile_dir=self.profile, resolution=resolution,
                chrome_binary=chrome_binary or os.path.join(self.root, "no-such-chrome"))
        assert self.rec.capture == "video"
        if pace:
            self.rec.pace_from(NARR)

    @property
    def webm(self):
        """The webm the fake Playwright wrote into record_video_dir (set once the browser was launched)."""
        return self.chromium.webm

    def _fake_run(self, argv, **kw):
        self.events.append("ffmpeg")
        self.run_calls.append((argv, kw))
        if self.run_exc:
            raise self.run_exc
        return types.SimpleNamespace(returncode=self.rc)

    def _tracked_mkdtemp(self, *a, **kw):
        kw.setdefault("dir", self.root)
        d = _REAL_MKDTEMP(*a, **kw)
        self.made_dirs.append(d)
        return d

    def run(self, body=None, rec=None):
        """Run one session(); body(env, page, cursor) runs inside it. Returns self with .err/.out/.seen set."""
        rec = rec or self.rec
        self.seen: dict = {}
        self.err = None
        out, err_out = io.StringIO(), io.StringIO()

        async def scene():
            async with rec.session() as (page, cursor):
                self.events.append("body")
                self.seen["video_t0_in_body"] = rec.video_t0
                if self.chromium.kwargs:
                    self.seen["video_dir_existed"] = os.path.isdir(self.chromium.kwargs["record_video_dir"])
                self.seen["cursor"] = (cursor.x, cursor.y)
                if body:
                    body(self, page, cursor)

        patches = [
            mock.patch.object(recorder_mod, "time", self.clock),
            mock.patch.object(recorder_mod.shutil, "which", lambda name: self.which),
            mock.patch.object(recorder_mod.subprocess, "run", self._fake_run),
            mock.patch.object(recorder_mod.tempfile, "mkdtemp", self._tracked_mkdtemp),
            # no_playwright: leave `playwright.async_api` unimportable (sys.modules["playwright"] is None here)
            mock.patch.dict(sys.modules, {} if self.no_playwright else {
                "playwright.async_api": types.SimpleNamespace(async_playwright=lambda: _PlaywrightCM(self.chromium))}),
        ]
        if self.aio is not None:
            patches.append(mock.patch.object(recorder_mod, "asyncio", self.aio))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err_out))
            try:
                asyncio.run(scene())
            except BaseException as e:  # noqa: BLE001 — the tests inspect it
                self.err = e
        self.out = out.getvalue()
        self.stderr = err_out.getvalue()
        return self


_REAL_MKDTEMP = tempfile.mkdtemp


def _dirs_gone(case, env):
    for d in env.made_dirs:
        case.assertFalse(os.path.exists(d), f"temp video dir leaked: {d}")


def _kept(case, env):
    """A failed convert keeps the ONLY copy of the take: the temp dir and the webm inside it survive.
    Every temp dir of a test lives under env.root, which the test's own cleanup removes (nothing in $TMPDIR)."""
    case.assertEqual(len(env.made_dirs), 1)
    d = env.made_dirs[0]
    case.assertTrue(os.path.isdir(d), "the temp video dir must be kept after a failed convert")
    case.assertTrue(os.path.isfile(env.webm), "the source webm must survive a failed convert")
    case.assertEqual(os.path.dirname(env.webm), d)
    case.assertTrue(d.startswith(env.root + os.sep))


# ---------------------------------------------------------------------------------------------

class TestSessionVideoLaunch(unittest.TestCase):
    def test_launch_kwargs(self):
        env = _Env(self, resolution="1280x720").run()
        self.assertIsNone(env.err)
        kw = env.chromium.kwargs
        self.assertIs(kw["headless"], True)
        self.assertEqual(kw["viewport"], {"width": 1280, "height": 720})
        self.assertEqual(kw["record_video_size"], {"width": 1280, "height": 720})
        self.assertEqual(kw["user_data_dir"], env.profile)
        self.assertTrue(os.path.isdir(env.profile), "the profile dir is created for the browser")
        self.assertIn("--disable-notifications", kw["args"])
        # the x11 path's window/kiosk/env knobs mean nothing for a headless viewport
        for key in ("no_viewport", "env", "ignore_default_args"):
            self.assertNotIn(key, kw)
        self.assertNotIn("--kiosk", kw["args"])

    def test_viewport_and_video_size_follow_the_resolution(self):
        env = _Env(self, resolution="1601x899").run()
        kw = env.chromium.kwargs
        self.assertEqual(kw["viewport"], {"width": 1601, "height": 899})
        self.assertEqual(kw["record_video_size"], {"width": 1601, "height": 899})
        self.assertEqual(env.seen["cursor"], (800.5, 449.5))

    def test_record_video_dir_is_a_fresh_dir_that_exists_during_the_session(self):
        env = _Env(self).run()
        kw = env.chromium.kwargs
        self.assertTrue(env.seen["video_dir_existed"])
        self.assertEqual(env.made_dirs, [kw["record_video_dir"]])
        self.assertNotEqual(kw["record_video_dir"], env.profile)

    def test_executable_path_is_none_when_chrome_binary_does_not_exist(self):
        env = _Env(self, chrome_binary="/definitely/not/here/google-chrome").run()
        self.assertIn("executable_path", env.chromium.kwargs)
        self.assertIsNone(env.chromium.kwargs["executable_path"])

    def test_executable_path_is_the_chrome_binary_when_the_file_exists(self):
        root = tempfile.mkdtemp(prefix="rdv-test-vcap-chrome-")
        self.addCleanup(shutil.rmtree, root, True)
        fake_chrome = os.path.join(root, "chrome")
        Path(fake_chrome).write_text("")
        env = _Env(self, chrome_binary=fake_chrome).run()
        self.assertEqual(env.chromium.kwargs["executable_path"], fake_chrome)

    def test_video_t0_is_taken_after_launch_returns_and_before_the_body(self):
        env = _Env(self)
        self.assertIsNone(env.rec.video_t0)
        env.run()
        self.assertEqual(env.clock.now, LAUNCH_AT, "premise: the fake launch moved the clock 100 -> 200")
        self.assertEqual(env.seen["video_t0_in_body"], LAUNCH_AT)
        self.assertEqual(env.rec.video_t0, LAUNCH_AT)

    def test_mark_works_inside_the_session_on_the_video_clock(self):
        def body(env, page, cursor):
            env.clock.now = LAUNCH_AT + 7.25
            env.rec.mark("inside")

        env = _Env(self).run(body)
        self.assertIsNone(env.err)
        self.assertEqual(env.out, "MARK 7.25 inside\n")

    def test_cursor_overlay_injected_and_mouse_parked_at_the_centre_before_the_body(self):
        env = _Env(self, resolution="1280x720").run()
        self.assertIn(("init_script", "cursor_overlay.js"), env.events)
        self.assertEqual(env.seen["cursor"], (640, 360))
        self.assertLess(env.events.index("new_page"), env.events.index(("mouse_move", 640, 360)))
        self.assertLess(env.events.index(("mouse_move", 640, 360)), env.events.index("body"))

    def test_second_take_starts_clean_and_the_log_follows_out_path(self):
        env = _Env(self, pace=True, fake_asyncio=True)     # fake sleep: take 1 ends with a 5 s cue hold

        def first(env, page, cursor):
            env.clock.now = LAUNCH_AT + 10.0
            env.rec.mark("open_form")

        env.run(first)
        self.assertIsNone(env.err)
        self.assertEqual(env.rec.ffmpeg_log, env.out_path + ".ffmpeg.log")
        take2 = os.path.join(env.root, "take2.mp4")
        env.rec.out_path = take2
        env.clock.now = 500.0
        seen = {}

        def second(env, page, cursor):
            seen["last_mark"] = env.rec._last_mark
            seen["cue_left"] = env.rec.cue_left()

        env.run(second)
        self.assertIsNone(env.err)
        self.assertIsNone(seen["last_mark"], "take 1's last mark must not leak into take 2")
        self.assertEqual(seen["cue_left"], 0.0)
        self.assertEqual(env.rec.ffmpeg_log, take2 + ".ffmpeg.log")
        self.assertEqual(env.run_calls[-1][0][-1], take2)


class TestSessionVideoFfmpeg(unittest.TestCase):
    def test_missing_ffmpeg_raises_runtime_error_before_the_browser_is_launched(self):
        env = _Env(self, which=None).run()
        self.assertIsInstance(env.err, RuntimeError)
        self.assertIn("ffmpeg", str(env.err))
        self.assertEqual(env.chromium.calls, 0, "launch must never be reached")
        self.assertNotIn("launch", env.events)
        self.assertNotIn("body", env.events)
        self.assertEqual(env.made_dirs, [], "no temp dir is created for a take that cannot start")
        self.assertEqual(env.run_calls, [])

    def test_normal_exit_converts_the_playwright_webm_into_out_path(self):
        env = _Env(self).run()
        self.assertIsNone(env.err)
        self.assertEqual(len(env.run_calls), 1)
        argv, kw = env.run_calls[0]
        self.assertEqual(argv[0], FFMPEG, "the resolved ffmpeg path is what runs")
        self.assertEqual(argv[argv.index("-i") + 1], env.webm, "input = page.video.path()")
        self.assertEqual(argv.count("-i"), 1)
        self.assertEqual(argv[-1], env.out_path)
        self.assertEqual(argv[argv.index("-c:v") + 1], "libx264")
        self.assertEqual(argv[argv.index("-pix_fmt") + 1], "yuv420p")
        self.assertIn("-y", argv)
        self.assertLess(argv.index("-i"), argv.index("-c:v"), "-i is the input; codec options follow it")
        self.assertIs(kw["stdin"], recorder_mod.subprocess.DEVNULL)
        for stream in ("stdout", "stderr"):
            self.assertEqual(os.path.abspath(kw[stream].name), os.path.abspath(env.out_path + ".ffmpeg.log"))
        self.assertTrue(os.path.exists(env.out_path + ".ffmpeg.log"))
        self.assertTrue(kw["stdout"].closed, "the log handle is closed once the convert is done")
        self.assertEqual(env.rec.ffmpeg_log, env.out_path + ".ffmpeg.log")

    def test_order_close_then_read_video_path_then_ffmpeg_then_dir_removed(self):
        env = _Env(self).run()
        tail = env.events[env.events.index("body"):]
        self.assertEqual(tail, ["body", "ctx_close", "video_path", "ffmpeg"])
        _dirs_gone(self, env)
        self.assertEqual(len(env.made_dirs), 1)
        self.assertFalse(os.path.exists(env.webm), "after a successful convert the source webm goes with the dir")

    def test_nonzero_ffmpeg_rc_on_a_good_body_raises_naming_the_log_and_keeps_the_webm(self):
        env = _Env(self, rc=1).run()
        self.assertIsInstance(env.err, RuntimeError)
        msg = str(env.err)
        self.assertIn("rc=1", msg)
        self.assertIn(env.out_path + ".ffmpeg.log", msg)
        self.assertIn("webm gốc giữ tại " + env.webm, msg)
        self.assertIsInstance(env.err.__cause__, RuntimeError, "chained `from exc`")
        self.assertIn("rc=1", str(env.err.__cause__))
        _kept(self, env)

    def test_ffmpeg_failing_to_spawn_on_a_good_body_raises_chained_and_keeps_the_webm(self):
        boom = FileNotFoundError("ffmpeg vanished")
        env = _Env(self, run_exc=boom).run()
        self.assertIsInstance(env.err, RuntimeError)
        self.assertIs(env.err.__cause__, boom)
        self.assertIn("ffmpeg vanished", str(env.err))
        self.assertIn("webm gốc giữ tại " + env.webm, str(env.err))
        _kept(self, env)

    def test_good_body_and_successful_convert_print_nothing_to_stderr(self):
        env = _Env(self).run()
        self.assertEqual(env.stderr, "")

    def test_good_body_with_a_failing_convert_still_raises_inside_a_caller_except_handler(self):
        # Regression: body_failed came from sys.exc_info(), which is also true while the
        # CALLER handles an unrelated exception — the failed convert was swallowed: no mp4, no error.
        try:
            raise KeyError("unrelated, already being handled")
        except KeyError:
            env = _Env(self, rc=1).run()
        self.assertIsInstance(env.err, RuntimeError)
        self.assertIn("rc=1", str(env.err))
        self.assertIn(env.webm, str(env.err))
        _kept(self, env)

    def test_good_body_and_good_convert_inside_a_caller_except_handler_is_clean(self):
        try:
            raise KeyError("unrelated, already being handled")
        except KeyError:
            env = _Env(self).run()
        self.assertIsNone(env.err)
        self.assertEqual(env.stderr, "")
        _dirs_gone(self, env)

    def test_out_path_in_a_missing_directory_fails_before_the_browser_is_launched(self):
        # Regression: the log was opened only after the take, so the whole take ran and
        # then FileNotFoundError deleted the webm. The log is now opened up front, like start_ffmpeg().
        env = _Env(self, out_name=os.path.join("no", "such", "dir", "take.mp4")).run()
        self.assertIsInstance(env.err, FileNotFoundError)
        self.assertEqual(env.chromium.calls, 0, "launch must never be reached")
        self.assertNotIn("body", env.events)
        self.assertEqual(env.made_dirs, [], "no temp dir for a take that cannot be saved")
        self.assertEqual(env.run_calls, [])

    def test_the_ffmpeg_log_exists_when_the_browser_is_launched(self):
        env = _Env(self)
        seen = {}
        env.chromium.on_launch = lambda kw: seen.update(log=os.path.exists(env.out_path + ".ffmpeg.log"))
        env.run()
        self.assertTrue(seen["log"], "the log is opened (truncated) BEFORE launch")

    def test_a_stale_log_from_an_earlier_take_is_truncated_up_front(self):
        env = _Env(self)
        Path(env.out_path + ".ffmpeg.log").write_text("old take noise\n")
        seen = {}
        env.chromium.on_launch = lambda kw: seen.update(size=os.path.getsize(env.out_path + ".ffmpeg.log"))
        env.run()
        self.assertEqual(seen["size"], 0)

    def test_playwright_not_importable_raises_runtime_error_pointing_at_the_right_interpreter(self):
        env = _Env(self, no_playwright=True).run()
        self.assertIsInstance(env.err, RuntimeError)
        msg = str(env.err)
        self.assertIn("playwright", msg.lower())
        self.assertIn("uvx", msg)
        self.assertIsInstance(env.err.__cause__, ImportError)
        self.assertEqual(env.chromium.calls, 0)
        self.assertEqual(env.made_dirs, [])
        self.assertEqual(env.run_calls, [])
        self.assertFalse(os.path.exists(env.out_path + ".ffmpeg.log"), "nothing is created for an unusable take")


class TestSessionVideoPopupWarning(unittest.TestCase):
    """Only the page session() yields is recorded; a second tab has its own webm that is dropped."""

    @staticmethod
    def _open_popup(env, page, cursor):
        env.chromium.ctx.pages.append(object())

    def test_a_popup_makes_session_print_a_stderr_line_naming_it(self):
        env = _Env(self).run(self._open_popup)
        self.assertIsNone(env.err)
        lines = env.stderr.splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("popup", lines[0])
        self.assertEqual(env.out, "", "the warning goes to stderr, never into the MARK stream")

    def test_one_page_prints_nothing(self):
        env = _Env(self).run()
        self.assertEqual(len(env.chromium.ctx.pages), 1, "premise: exactly the page session() created")
        self.assertEqual(env.stderr, "")

    def test_the_warning_is_also_printed_when_the_body_failed(self):
        def popup_then_boom(env, page, cursor):
            env.chromium.ctx.pages.append(object())
            raise ValueError("demo step failed")

        env = _Env(self).run(popup_then_boom)
        self.assertIsInstance(env.err, ValueError)
        self.assertIn("popup", env.stderr)


class TestSessionVideoFailures(unittest.TestCase):
    """A convert problem must never hide the demo script's own error, and nothing may leak."""

    def _boom(self, env, page, cursor):
        raise ValueError("demo step failed")

    def test_body_error_propagates_closes_the_ctx_and_removes_the_temp_dir(self):
        env = _Env(self).run(self._boom)
        self.assertIsInstance(env.err, ValueError)
        self.assertIn("demo step failed", str(env.err))
        self.assertIn("ctx_close", env.events)
        _dirs_gone(self, env)
        self.assertEqual(len(env.made_dirs), 1)

    def test_ffmpeg_failure_after_a_body_error_keeps_the_original_exception_and_the_webm(self):
        env = _Env(self, rc=1).run(self._boom)
        self.assertEqual(len(env.run_calls), 1, "premise: the convert was attempted, so the guard is exercised")
        self.assertIsInstance(env.err, ValueError)
        self.assertNotIsInstance(env.err, RuntimeError)
        self.assertIn("demo step failed", str(env.err))
        lines = [ln for ln in env.stderr.splitlines() if ln.startswith("record-demo-video:")]
        self.assertEqual(len(lines), 1, env.stderr)
        self.assertIn("webm gốc giữ tại " + env.webm, lines[0])
        self.assertIn("rc=1", lines[0])
        _kept(self, env)

    def test_ffmpeg_spawn_error_after_a_body_error_keeps_the_original_exception_and_the_webm(self):
        env = _Env(self, run_exc=FileNotFoundError("ffmpeg vanished")).run(self._boom)
        self.assertEqual(len(env.run_calls), 1, "premise: the convert was attempted")
        self.assertIsInstance(env.err, ValueError)
        self.assertIn("webm gốc giữ tại " + env.webm, env.stderr)
        _kept(self, env)

    def test_a_cancelled_body_counts_as_failed_too_baseexception_not_just_exception(self):
        def cancelled(env, page, cursor):
            raise asyncio.CancelledError()

        env = _Env(self, rc=1).run(cancelled)
        self.assertIsInstance(env.err, asyncio.CancelledError, "a failed convert must not replace the cancel")
        self.assertIn("webm gốc giữ tại " + env.webm, env.stderr)
        _kept(self, env)

    def test_body_error_inside_a_caller_except_handler_still_wins_over_a_convert_failure(self):
        try:
            raise KeyError("unrelated, already being handled")
        except KeyError:
            env = _Env(self, rc=1).run(self._boom)
        self.assertIsInstance(env.err, ValueError)
        _kept(self, env)

    def test_no_warning_line_when_the_convert_works_after_a_body_error(self):
        env = _Env(self).run(self._boom)
        self.assertEqual(env.stderr, "")

    def test_add_init_script_failure_closes_the_ctx_without_touching_the_unbound_page(self):
        env = _Env(self, init_fail=RuntimeError("overlay injection failed")).run()
        self.assertIsInstance(env.err, RuntimeError)
        self.assertIn("overlay injection failed", str(env.err), "not an UnboundLocalError/AttributeError on page")
        self.assertIn("ctx_close", env.events)
        self.assertNotIn("body", env.events)
        self.assertNotIn("video_path", env.events, "no page exists, so there is no video to read")
        self.assertEqual(env.run_calls, [], "nothing to convert")
        _dirs_gone(self, env)
        self.assertEqual(len(env.made_dirs), 1)

    def test_new_page_failure_closes_the_ctx_and_removes_the_temp_dir(self):
        env = _Env(self, new_page_fail=RuntimeError("no page")).run()
        self.assertIsInstance(env.err, RuntimeError)
        self.assertIn("no page", str(env.err))
        self.assertIn("ctx_close", env.events)
        self.assertEqual(env.run_calls, [])
        _dirs_gone(self, env)
        self.assertEqual(len(env.made_dirs), 1)

    def test_launch_failure_removes_the_temp_dir(self):
        env = _Env(self)

        async def boom(**kw):
            env.events.append("launch")
            raise RuntimeError("chrome crashed at start")

        env.chromium.launch_persistent_context = boom
        env.run()
        self.assertIsInstance(env.err, RuntimeError)
        self.assertIn("chrome crashed", str(env.err))
        self.assertIsNone(env.rec.video_t0, "no clock without a running browser")
        self.assertEqual(env.run_calls, [])
        _dirs_gone(self, env)
        self.assertEqual(len(env.made_dirs), 1)


class TestSessionVideoInterruptDuringConvert(unittest.TestCase):
    """Regression: KeyboardInterrupt/SystemExit are not `Exception`, so they slipped past
    the `except Exception` that used to set the keep-dir flag and the finally deleted the ONLY recording."""

    def _interrupted(self, exc, body=None):
        env = _Env(self, run_exc=exc).run(body)
        self.assertEqual(len(env.run_calls), 1, "premise: the convert was attempted, so the interrupt hit it")
        self.assertIs(env.err, exc, "the interrupt itself propagates, not a wrapper or the body's error")
        _kept(self, env)
        return env

    def test_keyboard_interrupt_during_the_convert_keeps_the_dir_and_the_webm(self):
        self._interrupted(KeyboardInterrupt("ctrl-c during ffmpeg"))

    def test_system_exit_during_the_convert_keeps_the_dir_and_the_webm(self):
        env = self._interrupted(SystemExit(3))
        self.assertEqual(env.err.code, 3)

    def test_keyboard_interrupt_during_the_convert_after_a_body_error_wins_and_keeps_the_dir(self):
        def boom(env, page, cursor):
            raise ValueError("demo step failed")

        env = self._interrupted(KeyboardInterrupt("ctrl-c during ffmpeg"), body=boom)
        self.assertNotIsInstance(env.err, ValueError)

    def test_system_exit_during_the_convert_after_a_body_error_wins_and_keeps_the_dir(self):
        def boom(env, page, cursor):
            raise ValueError("demo step failed")

        self._interrupted(SystemExit(1), body=boom)

    def test_control_a_convert_that_returns_removes_the_dir(self):
        env = _Env(self).run()
        self.assertIsNone(env.err)
        self.assertEqual(len(env.run_calls), 1)
        _dirs_gone(self, env)
        self.assertEqual(len(env.made_dirs), 1)

    def test_control_a_page_without_video_removes_the_dir_and_never_converts(self):
        env = _Env(self)
        real_launch = env.chromium.launch_persistent_context

        async def launch_without_video(**kw):
            ctx = await real_launch(**kw)

            async def new_page():
                page = _FakePage(ctx, env.events, env.chromium.webm, with_video=False)
                ctx.pages.append(page)
                return page

            ctx.new_page = new_page
            return ctx

        env.chromium.launch_persistent_context = launch_without_video
        env.run()
        self.assertIsNone(env.err)
        self.assertIn("ctx_close", env.events)
        self.assertNotIn("video_path", env.events)
        self.assertEqual(env.run_calls, [], "nothing to convert")
        self.assertEqual(env.stderr, "")
        _dirs_gone(self, env)
        self.assertEqual(len(env.made_dirs), 1, "premise: the dir existed and was removed, not never created")


class TestSessionVideoLastCue(unittest.TestCase):
    """The hold at the end of session() honours pace_from() in video mode too — normal exit only."""

    @staticmethod
    def _mark_open_form(env, page, cursor):
        env.clock.now = LAUNCH_AT + 10.0
        env.rec.mark("open_form")                 # needs 5 s (ko)
        env.clock.now += 1.0

    def test_normal_exit_waits_the_remaining_cue_while_the_browser_still_records(self):
        env = _Env(self, pace=True, fake_asyncio=True).run(self._mark_open_form)
        self.assertIsNone(env.err)
        self.assertEqual(len(env.aio.sleeps), 1)
        self.assertAlmostEqual(env.aio.sleeps[0], 4.0)          # 10 + 5 - 11
        i = env.events.index("body")
        self.assertEqual(env.events[i + 1][0], "sleep", env.events)
        self.assertEqual(env.events[i + 2:], ["ctx_close", "video_path", "ffmpeg"])

    def test_without_pace_from_the_hold_is_zero(self):
        env = _Env(self, pace=False, fake_asyncio=True).run(self._mark_open_form)
        self.assertIsNone(env.err)
        self.assertEqual(env.aio.sleeps, [0.0])

    def test_body_error_does_not_hold_the_take_open(self):
        def boom(env, page, cursor):
            self._mark_open_form(env, page, cursor)
            raise ValueError("demo step failed")

        env = _Env(self, pace=True, fake_asyncio=True).run(boom)
        self.assertIsInstance(env.err, ValueError)
        self.assertEqual(env.aio.sleeps, [], "a failing take must not wait out the cue")
        self.assertIn("ctx_close", env.events)


# ---------------------------------------------------------------------------------------------
# make_recorder on darwin / linux

class TestMakeRecorderPlatforms(unittest.TestCase):
    def mk(self, platform, available, driver="auto", **kw):
        err = io.StringIO()
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.object(sr, "stealth_available", lambda: available), \
                contextlib.redirect_stderr(err):
            rec = sr.make_recorder(":202", "/tmp/o.mp4", "/tmp/prof-x", driver=driver, **kw)
        self.stderr = err.getvalue()
        return rec

    def test_darwin_auto_returns_the_chrome_recorder_even_when_stealth_is_available(self):
        rec = self.mk("darwin", True, resolution="1280x720")
        self.assertIs(type(rec), sr.DemoRecorder)
        self.assertNotIsInstance(rec, sr.StealthRecorder)
        self.assertEqual(rec.capture, "video")
        self.assertEqual((rec.display, rec.out_path, rec.profile_dir, rec.resolution),
                         (":202", "/tmp/o.mp4", "/tmp/prof-x", "1280x720"))

    def test_darwin_auto_with_a_really_importable_invisible_playwright_module(self):
        # Not the stealth_available() patch: a fake module whose spec find_spec() sees.
        fake = types.ModuleType("invisible_playwright")
        fake.__spec__ = importlib.machinery.ModuleSpec("invisible_playwright", None)
        with mock.patch.dict(sys.modules, {"invisible_playwright": fake}), \
                mock.patch.object(sys, "platform", "darwin"):
            self.assertTrue(sr.stealth_available(), "premise: the fake makes it importable")
            rec = sr.make_recorder(":202", "/tmp/o.mp4", "/tmp/prof-x")
        self.assertIs(type(rec), sr.DemoRecorder)
        self.assertEqual(rec.capture, "video")

    def test_darwin_auto_prints_no_uvx_hint_either_way(self):
        for available in (True, False):
            with self.subTest(available=available):
                self.mk("darwin", available)
                self.assertEqual(self.stderr, "")

    def test_darwin_forced_stealth_raises_runtime_error_mentioning_macos(self):
        for available in (True, False):
            with self.subTest(available=available), self.assertRaises(RuntimeError) as cm:
                self.mk("darwin", available, driver="stealth")
            self.assertIn("macOS", str(cm.exception))

    def test_darwin_forced_chrome_is_the_video_recorder(self):
        for available in (True, False):
            with self.subTest(available=available):
                rec = self.mk("darwin", available, driver="chrome")
                self.assertIs(type(rec), sr.DemoRecorder)
                self.assertEqual(rec.capture, "video")
                self.assertEqual(self.stderr, "")

    def test_darwin_kw_rules_are_unchanged(self):
        rec = self.mk("darwin", True, seed=11, locale="vi-VN", chrome_binary="/opt/chrome", capture="x11")
        self.assertIs(type(rec), sr.DemoRecorder)
        self.assertEqual((rec.chrome_binary, rec.capture), ("/opt/chrome", "x11"), "explicit capture is honoured")
        self.assertFalse(hasattr(rec, "seed"), "stealth-only kw are dropped")
        with self.assertRaises(TypeError) as cm:
            self.mk("darwin", True, kisok=False)
        self.assertIn("kisok", str(cm.exception))

    def test_darwin_bad_driver_still_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.mk("darwin", True, driver="firefox")

    def test_linux_is_unchanged(self):
        st = self.mk("linux", True)
        self.assertIs(type(st), sr.StealthRecorder)
        self.assertEqual(self.stderr, "")
        forced = self.mk("linux", True, driver="stealth")
        self.assertIs(type(forced), sr.StealthRecorder)
        chrome = self.mk("linux", False)
        self.assertIs(type(chrome), sr.DemoRecorder)
        self.assertEqual(chrome.capture, "x11")
        self.assertIn("uvx --from invisible-playwright-mcp python", self.stderr)
        self.assertEqual(self.mk("linux", True, driver="chrome").capture, "x11")
        with self.assertRaises(RuntimeError) as cm:
            self.mk("linux", False, driver="stealth")
        self.assertNotIn("macOS", str(cm.exception))


# ---------------------------------------------------------------------------------------------
# make_recorder / DemoRecorder on win32 (plugin addition: Windows takes the macOS video path)

class TestWin32VideoPath(unittest.TestCase):
    mk = TestMakeRecorderPlatforms.mk

    def test_auto_returns_the_chrome_video_recorder_even_when_stealth_is_available(self):
        for available in (True, False):
            with self.subTest(available=available):
                rec = self.mk("win32", available, resolution="1280x720")
                self.assertIs(type(rec), sr.DemoRecorder)
                self.assertNotIsInstance(rec, sr.StealthRecorder)
                self.assertEqual(rec.capture, "video")
                self.assertEqual(self.stderr, "", "never the uvx / Xvfb hint on Windows")

    def test_forced_stealth_raises_and_names_windows(self):
        for available in (True, False):
            with self.subTest(available=available), self.assertRaises(RuntimeError) as cm:
                self.mk("win32", available, driver="stealth")
            self.assertIn("Windows", str(cm.exception))
            self.assertNotIn("uvx", str(cm.exception))

    def test_forced_chrome_is_the_video_recorder(self):
        rec = self.mk("win32", True, driver="chrome")
        self.assertIs(type(rec), sr.DemoRecorder)
        self.assertEqual(rec.capture, "video")

    def test_explicit_x11_capture_is_still_honoured(self):
        rec = self.mk("win32", True, capture="x11")
        self.assertEqual(rec.capture, "x11")

    def test_bad_driver_still_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.mk("win32", True, driver="firefox")

    def test_session_takes_the_video_branch_never_xvfb_or_x11grab(self):
        # session() branches on self.capture; on win32 "auto" resolves to "video", so the Xvfb and ffmpeg
        # grab starters must never be reached.
        with mock.patch.object(sys, "platform", "win32"):
            rec = sr.DemoRecorder(":202", "/tmp/o.mp4", "/tmp/prof-x")
        self.assertEqual(rec.capture, "video")
        for name in ("start_xvfb", "start_ffmpeg"):
            with self.subTest(starter=name), mock.patch.object(rec, name, side_effect=AssertionError(name)), \
                    mock.patch.object(rec, "_session_video") as video:
                video.return_value.__aenter__ = mock.AsyncMock(return_value=("page", "cursor"))
                video.return_value.__aexit__ = mock.AsyncMock(return_value=False)

                async def use():
                    async with rec.session() as pc:
                        return pc

                self.assertEqual(asyncio.run(use()), ("page", "cursor"))
                video.assert_called_once()

    def test_missing_ffmpeg_message_has_the_windows_install_command(self):
        with mock.patch.object(sys, "platform", "win32"):
            rec = sr.DemoRecorder(":202", "/tmp/o.mp4", "/tmp/prof-x")
        with mock.patch("shutil.which", return_value=None), self.assertRaises(RuntimeError) as cm:
            rec._ffmpeg_path()
        self.assertIn("winget install", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
