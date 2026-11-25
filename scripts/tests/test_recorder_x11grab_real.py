#!/usr/bin/env python3
"""End-to-end check of DemoRecorder.session() on a REAL Xvfb + ffmpeg x11grab + Chrome:

1. the X pointer is NOT in the video (regression): `-draw_mouse 0` was
   placed after `-i`, ffmpeg took it for an output option and ignored it, and the real X
   pointer — parked by Xvfb at screen centre, a black hand while Chrome's CDP hover is over a
   clickable element — was captured in every video (measured: 131 dark px at centre vs 0);
2. the default --kiosk window fills the Xvfb screen: no tab strip / address bar at the top of
   the frame, no black 1-px right/bottom edge, and innerWidth/innerHeight == W+1 x H+1 (Chrome is
   asked for +1 px because its window comes out 1 px short on Xvfb; click_helpers reads it).

The test page overrides the overlay's `cursor: none` with `cursor: pointer !important` so Chrome
really sets a hand cursor (as on an Odoo list row) — the capture must still not draw it.

Availability: Xvfb / ffmpeg / /usr/bin/google-chrome / playwright missing, or no free display →
prints one `SKIP-FILE: <reason>` line and exits 0 (run-tests.sh protocol). Own display number
(first free one in 250..299; never :9x — another session records there), own mkdtemp dir;
cleans only those. Also: the first click_box of a session scrolls by wheel inside the scroller
(browser mouse parked at the centre), and start_xvfb() raises on a busy display.
3. ffmpeg's output goes to `<out>.ffmpeg.log` (non-empty, closed after the
   session), the MERGED fd 1 + fd 2 of the process tree (`> run.log 2>&1`) has no ffmpeg
   progress and `grep ^MARK` finds every mark, and with pace_from() the session keeps
   recording until the last marked step had its reading time (ffprobe duration).

Run: python3 shared/skills/record-demo-video/scripts/tests/test_recorder_x11grab_real.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

W, H = 1280, 720
CHROME = "/usr/bin/google-chrome"
PAGE_RGB = (0xEE, 0xEE, 0xFF)
HOVER = (1100.0, 600.0)            # CDP mouse: far from the screen centre and the top strip
DARK = 100                         # gray < 100 = a cursor / bar / black-root pixel
TAIL_CUE = 2.5                     # pace_from min_cue for the last mark
HTML = ("<html><head><style>html,body{margin:0;height:100%;background:#eef}"
        "#a{position:fixed;inset:0;background:#eef;cursor:pointer !important}</style></head>"
        "<body><div id=a></div></body></html>")
# Odoo-17-like: fixed navbar (46) + control panel (58) over the ONLY scroller `.o_content` (from 104).
# A wheel with the pointer over the navbar scrolls nothing; logs every wheel point + scrollTop.
ODOO_FAR = ("<html><head><style>body{margin:0;font:16px sans-serif}</style></head><body>"
            "<div style='position:fixed;top:0;left:0;right:0;height:46px;background:#714B67'></div>"
            "<div style='position:fixed;top:46px;left:0;right:0;height:58px;background:#ddd'></div>"
            "<div id=oc style='position:absolute;top:104px;bottom:0;left:0;right:0;overflow:auto'>"
            "<div style='height:3000px'></div><button id=far style='height:40px'>FAR</button>"
            "<div style='height:600px'></div></div>"
            "<script>window.__w=[];window.__s=[];window.__c=[];"
            "addEventListener('wheel',e=>__w.push([e.clientX,e.clientY]),{capture:true});"
            "document.getElementById('oc').addEventListener('scroll',e=>__s.push(e.target.scrollTop));"
            "addEventListener('click',e=>__c.push(e.target.id),{capture:true});</script></body></html>")
FIRST_SCROLL_JS = "() => ({wheels: __w, scrolls: __s, clicks: __c})"


def max_jump(scrolls: list) -> float:
    prev, worst = 0.0, 0.0
    for s in scrolls:
        worst, prev = max(worst, abs(s - prev)), s
    return worst


def _free_display(exclude: str = "") -> str | None:
    for n in range(250, 300):
        if f":{n}" == exclude:
            continue
        if not os.path.exists(f"/tmp/.X{n}-lock") and not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return f":{n}"
    return None


def _duration(video: str) -> float:
    """Container duration via ffprobe — raises (CalledProcessError) on a broken / trailer-less mp4."""
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video],
                         capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    return float(out)


def _gray_frames(video: str, start: float, dur: float, crop: str, fps: int = 5) -> list[bytes]:
    w, h = (int(v) for v in crop.split(":")[:2])
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", video,
         "-vf", f"fps={fps},crop={crop}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True, timeout=60).stdout
    n = w * h
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]


class TestRealCapture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        recorder_spec = importlib.util.spec_from_file_location("recorder_x11grab_real_mod", SCRIPTS_DIR / "recorder.py")
        mod = importlib.util.module_from_spec(recorder_spec)
        recorder_spec.loader.exec_module(mod)
        from click_helpers import click_box
        cls.tmp = tempfile.mkdtemp(prefix="rdv-x11grab-")
        cls.video = os.path.join(cls.tmp, "out.mp4")
        cls.display = DISPLAY
        rec = mod.DemoRecorder(display=cls.display, out_path=cls.video,
                               profile_dir=os.path.join(cls.tmp, "profile"),
                               resolution=f"{W}x{H}", chrome_binary=CHROME, xvfb_startup_wait=0, capture="x11")
        orig_start_xvfb = rec.start_xvfb

        def checked_start_xvfb():
            # Our Xvfb must own the display — never capture another X server.
            orig_start_xvfb()
            sock = f"/tmp/.X11-unix/X{cls.display[1:]}"
            for _ in range(100):
                if rec._xvfb_proc.poll() is not None:
                    raise RuntimeError(f"Xvfb {cls.display} exited (rc={rec._xvfb_proc.returncode})")
                if os.path.exists(sock):
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError(f"Xvfb {cls.display} socket never appeared")
            with open(f"/tmp/.X{cls.display[1:]}-lock") as f:
                if int(f.read().strip()) != rec._xvfb_proc.pid:
                    raise RuntimeError(f"display {cls.display} is owned by another X server")

        rec.start_xvfb = checked_start_xvfb
        cls.info = {}

        async def scene():
            async with rec.session() as (page, cursor):
                # FIRST action of the session: a far target (no mouse move before it).
                await page.set_content(ODOO_FAR, wait_until="load")
                await click_box(page, page.locator("#far"), cursor, steps=5, duration=0.05)
                cls.info["first"] = await page.evaluate(FIRST_SCROLL_JS)
                await page.set_content(HTML, wait_until="load")
                for i in range(1, 11):     # CDP hover onto the pointer element
                    await page.mouse.move(HOVER[0] * i / 10, HOVER[1] * i / 10)
                    await asyncio.sleep(0.03)
                cls.info["cursor_css"] = await page.evaluate(
                    "([x,y]) => getComputedStyle(document.elementFromPoint(x,y)).cursor", list(HOVER))
                await asyncio.sleep(0.4)
                cls.info["t_hover"] = rec.video_elapsed()
                await asyncio.sleep(3.0)    # the hold window the frames are taken from
                # Read AFTER the window settled: right after load Chrome reports W x H, ~1 s
                # later W-1 x H-1 (= outerWidth/Height) — measured 3x at 1280x720 and 1920x1080.
                cls.info["inner"] = await page.evaluate("() => [innerWidth, innerHeight]")
                # the LAST marked step must stay in the video for its reading time.
                rec.mark("tail")
                cls.info["t_tail"] = rec.video_elapsed()
                cls.info["log_fh_open_inside"] = rec._ffmpeg_log_fh is not None and not rec._ffmpeg_log_fh.closed

        # TAIL_CUE s of reading time for "tail" (min_cue; the text is short) — session() must wait it out.
        rec.pace_from({"tail": {"vi": "Kết thúc", "en": "End"}}, min_cue=TAIL_CUE)
        # Capture the MERGED fd 1 + fd 2 of this process and every child (Xvfb, ffmpeg, Chrome) — the
        # stream a script run as `script.py > run.log 2>&1` produces. contextlib.redirect_stdout would only see Python's
        # own prints, never a child's fd writes (a pre-fix-stdio mutant stayed green).
        cap_path = os.path.join(cls.tmp, "merged.log")
        sys.stdout.flush()
        sys.stderr.flush()
        saved = (os.dup(1), os.dup(2))
        cap_fd = os.open(cap_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.dup2(cap_fd, 1)
            os.dup2(cap_fd, 2)
            asyncio.run(scene())
        except BaseException:
            shutil.rmtree(cls.tmp, ignore_errors=True)
            raise
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(saved[0], 1)
            os.dup2(saved[1], 2)
            for fd in (cap_fd, *saved):
                os.close(fd)
        with open(cap_path, "rb") as f:
            cls.merged = f.read().decode("utf-8", "replace")
        cls.ffmpeg_log = rec.ffmpeg_log
        cls.log_fh_after = rec._ffmpeg_log_fh

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _frames(self, crop):
        # Frames from the hold window (video time ≈ elapsed − ffmpeg start-up, < 1.8 s).
        frames = _gray_frames(self.video, self.info["t_hover"], 1.2, crop)
        self.assertGreaterEqual(len(frames), 3, "premise: the capture has frames in the hold window")
        return frames

    def test_first_click_box_wheels_inside_the_scroller_no_jump(self):
        # Regression: the browser mouse sat at (0,0) while
        # CursorState said centre → the first wheel hit the navbar (nothing scrolls) and the
        # fallback scrollIntoView jumped 3380 px in one event. session() now parks the mouse.
        first = self.info["first"]
        self.assertEqual(first["clicks"], ["far"])
        self.assertGreaterEqual(len(first["wheels"]), 2, first)
        self.assertGreater(first["wheels"][0][1], 104, f"first wheel over the navbar/panel: {first['wheels'][:2]}")
        self.assertLessEqual(max_jump(first["scrolls"]), 200, f"single scroll jump: {first['scrolls']}")

    def test_premise_chrome_shows_a_hand_cursor_over_the_page(self):
        self.assertEqual(self.info["cursor_css"], "pointer")

    def test_no_x_pointer_at_screen_centre_fix_draw_mouse_before_i(self):
        frames = self._frames(f"80:80:{W // 2 - 40}:{H // 2 - 40}")
        for k, fr in enumerate(frames):
            mean = sum(fr) / len(fr)
            self.assertGreater(mean, 200, f"frame {k}: the page is not visible at the centre (mean {mean:.0f})")
            dark = sum(1 for v in fr if v < DARK)
            self.assertEqual(dark, 0, f"frame {k}: {dark} dark px at screen centre — the X pointer was captured")

    def test_kiosk_window_has_no_tab_or_address_bar_in_the_frame(self):
        for k, fr in enumerate(self._frames(f"{W}:90:0:0")):
            dark = sum(1 for v in fr if v < DARK)
            self.assertEqual(dark, 0, f"frame {k}: {dark} dark px in the top 90 rows — browser chrome visible")
            self.assertGreater(min(fr), 200, f"frame {k}: the top strip is not the page background")

    def test_no_black_one_px_edge_right_column_and_bottom_row(self):
        # Regression (2026-10-21): Chrome's window comes out 1 px short of --window-size on
        # Xvfb (W-1 x H-1), so every frame had a black right column (720 px) + bottom row
        # (1280 px) of the bare root window. recorder.py now asks for W+1 x H+1.
        # 2-px strips (a 1-px crop is rejected by the yuv420p decoder path); both px must be page.
        for crop, what in ((f"2:{H}:{W - 2}:0", "2 right columns"), (f"{W}:2:0:{H - 2}", "2 bottom rows")):
            for k, fr in enumerate(self._frames(crop)):
                dark = sum(1 for v in fr if v < DARK)
                self.assertEqual(dark, 0, f"frame {k}: {dark} dark px in the {what}")

    # --- ffmpeg log split from the MARK stdout + last-cue wait ------------------
    def test_video_is_a_valid_mp4(self):
        dur = _duration(self.video)
        self.assertGreater(dur, self.info["t_tail"] - 1.0, f"video too short: {dur}")

    def test_ffmpeg_log_exists_nonempty_and_closed(self):
        self.assertEqual(self.ffmpeg_log, self.video + ".ffmpeg.log")
        self.assertTrue(self.info["log_fh_open_inside"], "premise: the log is open while recording")
        self.assertIsNone(self.log_fh_after, "stop_ffmpeg() must close the log")
        with open(self.ffmpeg_log, "rb") as f:
            log = f.read()
        self.assertGreater(len(log), 0)
        self.assertIn(b"frame=", log, "ffmpeg progress goes to the log")

    def test_merged_output_has_no_ffmpeg_progress_and_grep_finds_every_mark_fix(self):
        # Regression: with stdout+stderr in one log, ffmpeg's `\r` progress glued
        # onto MARK lines and `grep ^MARK` missed some. self.merged = fd 1 + fd 2 of the whole process tree.
        import build_srt
        self.assertNotIn("frame=", self.merged, "ffmpeg progress reached the process's stdout/stderr")
        grep_lines = [ln for ln in self.merged.split("\n") if ln.startswith("MARK ")]   # what `grep ^MARK` sees
        self.assertEqual([ln.split()[-1] for ln in grep_lines], ["tail"], grep_lines)
        self.assertEqual([m.step for m in build_srt.parse_marks(self.merged)], ["tail"])

    def test_session_keeps_recording_until_the_last_cue_was_readable(self):
        # Without the post-yield wait the video ends ~0.3 s after the mark (ctx close + 'q').
        dur = _duration(self.video)
        self.assertGreaterEqual(dur, self.info["t_tail"] + TAIL_CUE - 0.6,
                                f"video {dur:.2f}s ends before 'tail' (at {self.info['t_tail']:.2f}s) had {TAIL_CUE}s")

    def test_kiosk_page_area_is_the_full_recording_resolution(self):
        # Chrome is asked for W+1 x H+1 (it ends 1 px short on Xvfb) — inner covers the frame.
        # A normal window would lose ~88 px of height to the tab + address bar.
        iw, ih = self.info["inner"]
        self.assertEqual((iw, ih), (W + 1, H + 1), self.info)


class TestBusyDisplay(unittest.TestCase):
    """Regression (measured on :236): when the display is
    already in use, OUR Xvfb exits at once and ffmpeg + the browser silently recorded the OTHER
    session's screen. start_xvfb() now raises. A real Xvfb of this test holds the display."""

    def test_start_xvfb_on_a_busy_display_raises(self):
        spec = importlib.util.spec_from_file_location("recorder_busy_mod", SCRIPTS_DIR / "recorder.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        disp = _free_display(exclude=DISPLAY)
        if disp is None:
            self.fail("no second free display in :250..:299")
        holder = subprocess.Popen(["Xvfb", disp, "-screen", "0", "640x480x24", "-nolisten", "tcp"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        rec = mod.DemoRecorder(display=disp, out_path="/dev/null", profile_dir="/tmp/unused-prof",
                               resolution="640x480", xvfb_startup_wait=1.5, capture="x11")
        try:
            for _ in range(100):
                if os.path.exists(f"/tmp/.X11-unix/X{disp[1:]}"):
                    break
                time.sleep(0.05)
            self.assertIsNone(holder.poll(), "premise: the holder Xvfb runs")
            with self.assertRaises(RuntimeError) as cm:
                rec.start_xvfb()
            self.assertIn(disp, str(cm.exception))
            self.assertIsNotNone(rec._xvfb_proc.poll(), "our Xvfb exited (display busy)")
        finally:
            if rec._xvfb_proc is not None and rec._xvfb_proc.poll() is None:
                rec._xvfb_proc.terminate()
                rec._xvfb_proc.wait(timeout=5)
            holder.terminate()
            holder.wait(timeout=5)


DISPLAY: str = ""


def main() -> int:
    global DISPLAY
    t0 = time.monotonic()
    missing = [b for b in ("Xvfb", "ffmpeg", "ffprobe") if shutil.which(b) is None]
    if not os.path.exists(CHROME):
        missing.append(CHROME)
    if missing:
        print(f"SKIP-FILE: not installed: {', '.join(missing)}")
        return 0
    try:
        import playwright.async_api  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"SKIP-FILE: playwright not importable ({type(e).__name__}: {e})"[:300])
        return 0
    DISPLAY = _free_display() or ""
    if not DISPLAY:
        print("SKIP-FILE: no free X display in :250..:299")
        return 0
    prog = unittest.main(exit=False)
    print(f"x11grab real file time: {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return 0 if prog.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
