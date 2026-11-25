#!/usr/bin/env python3
"""The real stealth e2e must never leave orphans behind when its scene times out:
the scene's Python is a child of `uv tool uvx`, so killing only uvx left
the scene, Xvfb, ffmpeg and Firefox running (and the display locked).

Runs stealth_scene.py with RDV_SCENE_HANG=1 (it hangs inside StealthRecorder.session() with the
whole stack up) through test_stealth_x11grab_real.run_scene(ready="SCENE_HANGING"): the kill
fires KILL_AFTER s after the stack is UP (not a fixed time after the uvx launch, which a loaded
machine could overrun). Then checks that every process of the group is gone and Xvfb released
its display lock, all within a bounded time (a cleanup regression fails, it never freezes).

Availability: same SKIP-FILE rules as test_stealth_x11grab_real.py; own display in :230..:249
(files run in parallel: the capture test uses :200..:229, the Chrome x11grab test :250..:299),
own mkdtemp dir.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_stealth_timeout_cleanup_real.py
"""
from __future__ import annotations

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
_spec = importlib.util.spec_from_file_location("stealth_real_helpers", HERE / "test_stealth_x11grab_real.py")
assert _spec and _spec.loader
rt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rt)

READY_TIMEOUT = 60.0     # stack up = `SCENE_HANGING` on stdout (~3.3 s idle; generous for a loaded box)
KILL_AFTER = 1.0         # the timeout then fires this long after the stack is up
DISPLAY = ""


class TestTimeoutKillsTheWholeGroup(unittest.TestCase):
    def test_timed_out_scene_leaves_no_process_and_no_display_lock(self):
        tmp = tempfile.mkdtemp(prefix="rdv-stealth-hang-")
        env = {**rt.ENV, "RDV_SCENE_HANG": "1"}
        t0 = time.monotonic()
        try:
            with self.assertRaises(rt.SceneTimeout) as cm:
                rt.run_scene(rt.UVX + [str(rt.SCENE), str(rt.SCRIPTS_DIR), DISPLAY, tmp], env, READY_TIMEOUT,
                             ready="SCENE_HANGING", kill_after=KILL_AFTER)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        e = cm.exception
        try:
            self.assertIn("SCENE_HANGING", e.out, f"premise: the whole stack was up when the timeout fired ({e})")
            cmds = " | ".join(c for _p, c in e.group)
            for name in ("python", "Xvfb", "ffmpeg", "firefox"):
                self.assertIn(name, cmds, f"premise: {name} was in the process tree")
            survivors = [(p, c) for p, c in e.group if rt.alive(p)]
            self.assertEqual(survivors, [], f"orphans left after the timeout: {e}")
            pgid = [p for p, c in e.group if c == "uvx"][0]
            with self.assertRaises(ProcessLookupError):
                os.killpg(pgid, 0)
            self.assertFalse(os.path.exists(f"/tmp/.X{DISPLAY[1:]}-lock"), "Xvfb did not release its display lock")
            # Bounded: ready + KILL_AFTER + TERM/KILL grace (≤5 s) + pipe drain (≤5 s) — never a frozen gate.
            self.assertLess(time.monotonic() - t0, READY_TIMEOUT + KILL_AFTER + 11)
        finally:
            for pid, _c in e.group:          # a failing run must not leak processes into the machine
                if rt.alive(pid):
                    try:
                        os.kill(pid, 9)
                    except OSError:
                        pass


def main() -> int:
    global DISPLAY
    t0 = time.monotonic()
    missing = [b for b in ("uvx", "Xvfb", "ffmpeg") if shutil.which(b) is None]
    if missing:
        print(f"SKIP-FILE: not installed: {', '.join(missing)}")
        return 0
    try:
        p = subprocess.run(rt.UVX + ["-c", "import invisible_playwright"], env=rt.ENV, capture_output=True,
                           text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("SKIP-FILE: uvx resolution of invisible-playwright-mcp timed out")
        return 0
    if p.returncode != 0:
        print("SKIP-FILE: invisible-playwright-mcp env not resolvable offline")
        return 0
    # Own range: the runner runs files in PARALLEL — :200-229 capture test, :250-299 Chrome x11grab.
    DISPLAY = rt._free_display(230, 250) or ""
    if not DISPLAY:
        print("SKIP-FILE: no free X display in :230..:249")
        return 0
    prog = unittest.main(exit=False)
    print(f"timeout-cleanup file time: {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return 0 if prog.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
