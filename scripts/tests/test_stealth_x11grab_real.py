#!/usr/bin/env python3
"""End-to-end check of the DEFAULT driver (StealthRecorder: invisible_playwright stealth Firefox)
on a REAL Xvfb + ffmpeg x11grab.

The stealth package only exists in the plugin env, so this file (system python3, like the
runner) runs tests/stealth_scene.py through `uvx --from invisible-playwright-mcp python` in a
subprocess and asserts on its JSON + the recorded video:
- make_recorder(driver="auto") picks StealthRecorder; DISPLAY is ours inside, restored after;
- click_box clicks, type_into REPLACES the text ("old" → "new"; uppercase `ControlOrMeta+A`
  gave "oldnew" in stealth Firefox), point_at does not click, a far target is reached by wheel;
- the frame: kiosk (no browser bar in the top rows), no black 1-px right/bottom edge, the
  library-drawn arrow present at the cursor, no X pointer at the screen centre;
- a second, short scene (seed 20, persona screen 2560x1440 at DPR 1.25) proves the kiosk page follows
  the Xvfb size at DPR 1, not the fingerprint persona's screen or DPR.

Availability: uvx / Xvfb / ffmpeg missing, the plugin env not resolvable OFFLINE (UV_OFFLINE=1:
no network in a test), or no free display in :200..:229 → one `SKIP-FILE: <reason>` line, exit 0.
Own display (never :9x — another session records there) + own mkdtemp dir, cleaned after.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_stealth_x11grab_real.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
SCENE = HERE / "stealth_scene.py"
W, H = 1280, 720
DARK = 100
UVX = ["uvx", "--from", "invisible-playwright-mcp", "python"]
ENV = {**os.environ, "UV_OFFLINE": "1", "DISPLAY": ":1999"}   # sentinel DISPLAY: must come back unchanged
DISPLAY = ""
SCENE_TIMEOUT = 120


def _free_display(lo: int = 200, hi: int = 250) -> str | None:
    for n in range(lo, hi):
        if not os.path.exists(f"/tmp/.X{n}-lock") and not os.path.exists(f"/tmp/.X11-unix/X{n}"):
            return f":{n}"
    return None


def descendants(root: int) -> list[tuple[int, str]]:
    """(pid, cmdline head) of every live descendant of `root`, via /proc (Linux)."""
    kids: dict[int, list] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ")[:60].decode(errors="replace")
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append((int(d), cmd))
    out, todo = [], [root]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c[0])
    return out


def group_members(pgid: int) -> list[tuple[int, str]]:
    """(pid, cmdline head) of every live process in process group `pgid` (reparented ones too)."""
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                fields = f.read().rsplit(")", 1)[1].split()
            if int(fields[2]) != pgid or fields[0] == "Z":
                continue
            with open(f"/proc/{d}/cmdline", "rb") as f:
                out.append((int(d), f.read().replace(b"\0", b" ")[:60].decode(errors="replace")))
        except (OSError, IndexError, ValueError):
            continue
    return out


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class SceneTimeout(AssertionError):
    def __init__(self, msg, out, group, survivors=()):
        super().__init__(msg)
        self.out, self.group, self.survivors = out, group, list(survivors)


PIPE_DRAIN = 5.0         # after the group kill: bounded wait for the pipes (no frozen commit gate)


def run_scene(argv: list, env: dict, timeout: float, ready: str | None = None,
              kill_after: float = 1.0) -> tuple[int, str, str]:
    """Run the uvx scene in its OWN session/process group; stdout/stderr are drained by threads.

    ready=None: wait up to `timeout` s for the scene to finish. ready="<prefix>": wait up to
    `timeout` s for a stdout line starting with it, then kill the group `kill_after` s later
    (the timeout-cleanup test: the clock starts when the stack is UP, not at the uvx launch).

    Killing = the WHOLE group (the scene's Python is a child of `uv tool uvx` and survives a plain
    kill of uvx, keeping Xvfb, ffmpeg and Firefox alive): SIGTERM first so Xvfb
    removes its lock file, then SIGKILL. Then the pipes get PIPE_DRAIN s at most: a survivor still
    holding them raises SceneTimeout naming it instead of hanging the gate."""
    p = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
    out_lines: list[str] = []
    err_chunks: list[str] = []
    seen = threading.Event()

    def read_out():
        for line in p.stdout:
            out_lines.append(line)
            if ready and line.startswith(ready):
                seen.set()

    def read_err():
        for chunk in p.stderr:
            err_chunks.append(chunk)

    readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in readers:
        t.start()

    if ready is not None:
        if seen.wait(timeout):
            time.sleep(kill_after)
            reason = f"killed {kill_after}s after {ready!r}"
        else:
            reason = f"{ready!r} not seen within {timeout}s"
    else:
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            reason = f"scene timed out after {timeout}s"
        else:
            deadline = time.monotonic() + PIPE_DRAIN
            for t in readers:
                t.join(max(0.0, deadline - time.monotonic()))
            if not any(t.is_alive() for t in readers):
                return p.returncode, "".join(out_lines), "".join(err_chunks)
            reason = f"scene exited rc={p.returncode} but a child kept its pipes open {PIPE_DRAIN}s later"

    group = list(dict.fromkeys(descendants(p.pid) + group_members(p.pid) + [(p.pid, "uvx")]))
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(p.pid, sig)
        except ProcessLookupError:
            break
        t0 = time.monotonic()
        while time.monotonic() - t0 < grace and any(alive(pid) for pid, _ in group):
            time.sleep(0.1)
    try:
        p.wait(timeout=PIPE_DRAIN)
    except subprocess.TimeoutExpired:
        pass
    deadline = time.monotonic() + PIPE_DRAIN
    for t in readers:
        t.join(max(0.0, deadline - time.monotonic()))
    survivors = [(pid, c) for pid, c in group if alive(pid)]
    if any(t.is_alive() for t in readers) or survivors:
        reason += f"; group kill left survivors holding the pipes: {survivors}"
    else:
        reason += "; group killed"
    raise SceneTimeout(reason, "".join(out_lines), group, survivors)


def _gray_frames(video: str, start: float, dur: float, crop: str, fps: int = 5) -> list[bytes]:
    w, h = (int(v) for v in crop.split(":")[:2])
    raw = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", video,
         "-vf", f"fps={fps},crop={crop}", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True, timeout=60).stdout
    n = w * h
    return [raw[i:i + n] for i in range(0, len(raw) - n + 1, n)]


def record_scene(cls, display: str, extra_env: dict | None = None) -> None:
    """Run the scene into a fresh mkdtemp dir and set cls.tmp/wall/info/video/out/err (dir removed on failure)."""
    cls.tmp = tempfile.mkdtemp(prefix="rdv-stealth-")
    t = time.monotonic()
    try:
        rc, out, err = run_scene(UVX + [str(SCENE), str(SCRIPTS_DIR), display, cls.tmp],
                                 {**ENV, **(extra_env or {})}, SCENE_TIMEOUT)
    except BaseException:
        shutil.rmtree(cls.tmp, ignore_errors=True)
        raise
    cls.wall = time.monotonic() - t
    line = [ln for ln in out.splitlines() if ln.startswith("SCENE_JSON ")]
    if rc != 0 or not line:
        shutil.rmtree(cls.tmp, ignore_errors=True)
        raise AssertionError(f"scene failed rc={rc}\n{out[-1500:]}\n{err[-3000:]}")
    cls.info = json.loads(line[-1][len("SCENE_JSON "):])
    cls.video = os.path.join(cls.tmp, "raw.mp4")
    cls.out, cls.err = out, err      # the scene's stdout and stderr pipes (ffmpeg would inherit stderr)


class TestStealthKioskFollowsXvfbNotPersona(unittest.TestCase):
    """Regression (2026-11-06): since invisible-playwright 0.45.0 / engine
    build 20261104 a `--kiosk` window takes the fingerprint persona's screen size (seed 5 → 2560x1440)
    instead of the Xvfb size, so everything past the Xvfb edge silently left the video.
    StealthRecorder pins screen.* to the resolution. The seed-7 scene below cannot catch a resolution
    that happens to equal its persona (1920x1080 @1), so this one uses seed 20 at W x H: a short geometry
    scene with a fixed black square in the page's bottom-right corner, which must be IN the frame.

    Seed 20's persona is 2560x1440 at DPR 1.25 (invisible-core 51.48.0, 54.49.0, 55.50.0), so it also
    guards the DPR half: pinning only screen.width/height left the persona DPR, the
    page read 1280x720 / 1.25 = 1024x576 CSS px, rendered zoomed 125 %, while page.viewport_size still
    said 1280x720 — the value click_helpers.viewport_dims() trusts. StealthRecorder pins screen.dpr 1.0."""
    SEED = 20

    @classmethod
    def setUpClass(cls):
        # Same range as main()'s DISPLAY. unittest runs classes by name, so this one runs FIRST and normally
        # takes the display main() reserved for TestStealthRealCapture; that is free again once this
        # scene's Xvfb has exited.
        display = _free_display(200, 230)
        if not display:
            raise AssertionError("no free X display in :200..:229 for the geometry scene")
        record_scene(cls, display, {"RDV_SCENE_MODE": "geometry", "RDV_SCENE_SEED": str(cls.SEED)})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_kiosk_frame_follows_xvfb_not_persona_screen_regression(self):
        i = self.info
        self.assertEqual(i["seed"], self.SEED, "premise: the recorder took the seed")
        # Premises fail RED, never green: an unverifiable persona or a seed that cannot catch a regression.
        self.assertIsInstance(i["persona_screen"], list, f"premise unverifiable: {i['persona_screen']}")
        self.assertIsInstance(i["persona_dpr"], (int, float), f"premise unverifiable: {i['persona_dpr']}")
        self.assertNotEqual(i["persona_screen"], [W, H], f"premise: seed {self.SEED}'s persona screen must "
                            "differ from the recording size — pick another seed")
        self.assertNotEqual(i["persona_dpr"], 1, f"premise: seed {self.SEED}'s persona DPR must not be 1 — "
                            "pick another seed (public invisible_core.generate_profile(seed).screen.dpr)")
        why = f"persona {i['persona_screen']} @{i['persona_dpr']}, versions {i['versions']}"
        # One dict so a red shows every field: inner == viewport_size is what viewport_dims() relies on.
        got = {k: i[k] for k in ("inner", "viewport_size", "dpr", "screen")}
        self.assertEqual(got, {"inner": [W, H], "viewport_size": [W, H], "dpr": 1, "screen": [W, H]},
                         f"the kiosk page must be the Xvfb size at DPR 1, not the persona screen/DPR ({why})")
        cb = i["corner_box"]
        self.assertEqual([cb["x"] + cb["width"], cb["y"] + cb["height"]], [W, H], why)
        # The video itself: the page's bottom-right corner square is captured (it is off-screen when the
        # window is larger than the Xvfb screen).
        n = 40                                         # inside the 48 px square, clear of its antialiased edge
        fr = _gray_frames(self.video, i["t_corner"] + 0.2, 1.0, f"{n}:{n}:{W - n}:{H - n}")
        self.assertGreaterEqual(len(fr), 3, "premise: frames inside the corner hold window")
        for k, f in enumerate(fr):
            self.assertGreaterEqual(sum(v < DARK for v in f), n * n * 0.95,
                                    f"frame {k}: the page's bottom-right corner is not in the video ({why})")


class TestStealthRealCapture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        record_scene(cls, DISPLAY)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def frames(self, crop):
        fr = _gray_frames(self.video, self.info["t_point"] + 0.2, 1.0, crop)
        self.assertGreaterEqual(len(fr), 3, "premise: frames inside the point_at hold window")
        return fr

    # --- driver selection + environment ---------------------------------------------------
    def test_auto_driver_is_stealth_with_centre_native_cursor(self):
        self.assertEqual(self.info["recorder"], "StealthRecorder")
        self.assertEqual(self.info["cursor_start"], [W / 2, H / 2, True])

    def test_display_is_ours_inside_and_restored_after(self):
        self.assertEqual(self.info["display_before"], ":1999")
        self.assertEqual(self.info["display_inside"], DISPLAY)
        self.assertEqual(self.info["display_after"], ":1999")

    # --- helpers on the real stealth browser ------------------------------------------------
    def test_point_at_does_not_click_and_parks_beside_the_value(self):
        self.assertEqual(self.info["clicks_after_point"], ["b"])
        vb, (px, py) = self.info["v_box"], self.info["point"]
        right = vb["x"] + vb["width"]
        self.assertGreater(px, right)
        self.assertLessEqual(px - right, 17)

    def test_clicks_land_and_type_into_replaces_text(self):
        self.assertEqual(self.info["clicks"], ["b", "i", "far"])
        self.assertEqual(self.info["input"], "new", "select-all failed: the new text was appended")

    def test_far_target_reached_by_wheel_and_fully_in_frame(self):
        self.assertGreater(self.info["scroll_y"], 1000)
        fb = self.info["far_box"]
        self.assertGreaterEqual(fb["y"], 0)
        self.assertLessEqual(fb["y"] + fb["height"], H)

    def test_first_click_box_wheels_inside_the_scroller_no_jump(self):
        # Regression: the browser mouse sat at (0,0) while
        # CursorState said centre → the first wheel hit the navbar and scrollIntoView jumped
        # 3380 px in one event. session() now parks the browser mouse at the centre.
        first = self.info["first"]
        self.assertEqual(first["clicks"], ["far0"])
        self.assertGreaterEqual(len(first["wheels"]), 2, first)
        self.assertGreater(first["wheels"][0][1], 104, f"first wheel over the navbar/panel: {first['wheels'][:2]}")
        prev, worst = 0.0, 0.0
        for s in first["scrolls"]:
            worst, prev = max(worst, abs(s - prev)), s
        self.assertLessEqual(worst, 200, f"single scroll jump: {first['scrolls']}")

    def test_one_mouse_move_call_per_target_native_motion(self):
        # The scene wraps page.mouse.move and counts OUR calls per helper action. Native motion =
        # one call per target (the library draws the human path); 30-step easing would show 30.
        # (Wall time is only logged — see main(): ~13 s native vs ~52-65 s with 30 steps.)
        self.assertTrue(self.info["mouse_object_stable"], "premise: page.mouse is one object, the wrap holds")
        self.assertEqual(self.info["move_calls"], {
            "click_box_far_first": 1,        # cursor already inside the scroller: wheel, then 1 move
            "click_box_navbar": 1,
            "click_box_far_relocated": 2,    # cursor over the navbar: relocation into the scroller + click
            "scroll_through": 1,             # to the centre, then wheels only
            "click_box_visible": 1,
            "point_at": 1,
            "type_into": 1,                  # click_box + keyboard
            "click_box_far_window": 1,
        })
        self.assertEqual(self.info["odoo_clicks"], ["far0", "nav", "far1"])

    # --- point_at / log split ---------------------------------------------------------------------
    def test_point_at_full_width_row_parks_beside_its_first_text_fix(self):
        tb, rb, (px, py) = self.info["wide_text_box"], self.info["wide_row_box"], self.info["wide_point"]
        self.assertGreater(rb["width"], W / 2, "premise: the row is wider than half the page")
        right = tb["x"] + tb["width"]
        self.assertGreater(px, right)
        self.assertLessEqual(px - right, 17)
        self.assertAlmostEqual(py, tb["y"] + tb["height"] / 2, delta=1.5)
        self.assertEqual(self.info["wide_clicks"], [])

    def test_point_at_truncated_first_cell_parks_beside_the_visible_cell_end_fix_review_r1(self):
        cell, tb, st = self.info["trunc_cell_box"], self.info["trunc_text_box"], self.info["trunc_state_box"]
        px, py = self.info["trunc_point"]
        right = cell["x"] + cell["width"]
        self.assertGreater(tb["x"] + tb["width"], st["x"], "premise: the full text runs past the state cell")
        self.assertGreater(px, right - 0.5)
        self.assertLessEqual(px - right, 17)
        self.assertLess(px, st["x"], "the pointer is on another column's value")
        self.assertAlmostEqual(py, tb["y"] + tb["height"] / 2, delta=1.5)

    def test_ffmpeg_output_in_its_own_log_closed_after(self):
        self.assertEqual(self.info["ffmpeg_log"], os.path.join(self.tmp, "raw.mp4") + ".ffmpeg.log")
        self.assertGreater(self.info["ffmpeg_log_size"], 0)
        self.assertFalse(self.info["ffmpeg_log_fh_after"], "stop_ffmpeg() must close the log")

    def test_merged_output_has_no_ffmpeg_progress_and_grep_finds_the_mark_fix(self):
        # BOTH streams of the scene process tree (a script run with `2>&1` merges them): ffmpeg inherits stderr when
        # it is not redirected, so asserting on stdout alone could never see its progress.
        merged = self.out + self.err
        self.assertNotIn("frame=", merged, "ffmpeg progress reached the scene's stdout/stderr")
        grep_lines = [ln for ln in merged.split("\n") if ln.startswith("MARK ")]     # what `grep ^MARK` sees
        self.assertEqual(len(grep_lines), 1, grep_lines)
        self.assertRegex(grep_lines[0], r"^MARK \d+\.\d\d scene_end$")

    def test_kiosk_page_is_the_full_frame(self):
        self.assertEqual(self.info["inner"], [W, H], self.info["versions"])
        self.assertEqual(self.info["screen"], [W, H], self.info["versions"])

    # --- the recorded frames ----------------------------------------------------------------
    def test_no_browser_bar_in_the_top_rows(self):
        for k, fr in enumerate(self.frames(f"{W}:90:0:0")):
            self.assertEqual(sum(v < DARK for v in fr), 0, f"frame {k}: dark px in the top 90 rows")
            self.assertGreater(min(fr), 200, f"frame {k}: the top strip is not the page background")

    def test_no_black_edge_right_and_bottom(self):
        for crop, what in ((f"2:{H}:{W - 2}:0", "2 right columns"), (f"{W}:2:0:{H - 2}", "2 bottom rows")):
            for k, fr in enumerate(self.frames(crop)):
                self.assertEqual(sum(v < DARK for v in fr), 0, f"frame {k}: dark px in the {what}")

    def test_drawn_arrow_is_at_the_cursor(self):
        px, py = (int(round(v)) for v in self.info["point"])
        for k, fr in enumerate(self.frames(f"32:32:{px - 2}:{py - 2}")):
            self.assertGreaterEqual(sum(v < DARK for v in fr), 15, f"frame {k}: no drawn arrow at {px},{py}")

    def test_no_x_pointer_at_screen_centre(self):
        for k, fr in enumerate(self.frames(f"80:80:{W // 2 - 40}:{H // 2 - 40}")):
            self.assertEqual(sum(v < DARK for v in fr), 0, f"frame {k}: dark px at the screen centre")


def main() -> int:
    global DISPLAY
    t0 = time.monotonic()
    missing = [b for b in ("uvx", "Xvfb", "ffmpeg") if shutil.which(b) is None]
    if missing:
        print(f"SKIP-FILE: not installed: {', '.join(missing)}")
        return 0
    try:
        p = subprocess.run(UVX + ["-c", "import invisible_playwright"], env=ENV, capture_output=True,
                           text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("SKIP-FILE: uvx resolution of invisible-playwright-mcp timed out")
        return 0
    if p.returncode != 0:
        print(f"SKIP-FILE: invisible-playwright-mcp env not resolvable offline ({(p.stderr or '').strip()[-160:]})")
        return 0
    probe_secs = time.monotonic() - t0
    # Own range — files run in PARALLEL: :230-249 timeout-cleanup test, :250-299 Chrome x11grab.
    DISPLAY = _free_display(200, 230) or ""
    if not DISPLAY:
        print("SKIP-FILE: no free X display in :200..:229")
        return 0
    prog = unittest.main(exit=False)
    wall = getattr(TestStealthRealCapture, "wall", float("nan"))
    inside = getattr(TestStealthRealCapture, "info", {}).get("scene_secs", float("nan"))
    print(f"stealth real file time: {time.monotonic() - t0:.1f}s (uvx probe {probe_secs:.1f}s, scene {wall:.1f}s, "
          f"session {inside}s — logged only; native ~13 s, 30-step easing ~52-65 s)", file=sys.stderr)
    print(f"stealth versions: {getattr(TestStealthRealCapture, 'info', {}).get('versions')}", file=sys.stderr)
    return 0 if prog.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
