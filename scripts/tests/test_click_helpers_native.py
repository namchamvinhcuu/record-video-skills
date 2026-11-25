#!/usr/bin/env python3
"""Native-motion mode of click_helpers.py — no browser.

The stealth driver (invisible_playwright) animates a SINGLE `page.mouse.move(x, y)` along a
human-like curve itself (measured: 1 call → 19 mousemove events over 0.57 s). Our 30-step easing
on top of it would chain 30 humanized moves — slow and jerky. So `CursorState(native_motion=True)`
makes every helper call `smooth_move(..., native=True)` = exactly ONE move per target.

Reuses the page twin of test_click_helpers_scroll.py (World / Page / Locator / El): it records
every mouse.move / wheel / down / up / key press in `World.events`.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_click_helpers_native.py
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("click_helpers_native_twin", HERE / "test_click_helpers_scroll.py")
assert _spec and _spec.loader
tw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tw)
ch = tw.ch                      # the click_helpers module the twin's identity checks are bound to
W, H, VIS_TOP = tw.W, tw.H, tw.VIS_TOP
World, El, Page, Locator, run, moves, wheels = tw.World, tw.El, tw.Page, tw.Locator, tw.run, tw.moves, tw.wheels


def kinds(w):
    return [e[0] for e in w.events if e[0] in ("move", "down", "up", "wheel", "press", "type")]


class TestSmoothMove(unittest.TestCase):
    def test_native_is_exactly_one_move_to_the_target_no_sleep(self):
        w = World()
        run(w, ch.smooth_move(Page(w), 10, 20, 700, 300, native=True))
        self.assertEqual(moves(w), [(700, 300)])
        self.assertEqual(w.sleeps, [], "the library paces the motion — no sleeps of ours")

    def test_non_native_keeps_the_eased_steps(self):
        w = World()
        run(w, ch.smooth_move(Page(w), 0, 0, 400, 200, steps=4, duration=0.4))
        mv = moves(w)
        self.assertEqual(len(mv), 4)
        self.assertEqual(mv[-1], (400, 200))
        self.assertAlmostEqual(mv[0][0], 400 * ch.ease_out_cubic(0.25))
        self.assertEqual(w.sleeps, [0.1] * 4)

    def test_default_is_not_native_30_steps(self):
        w = World()
        run(w, ch.smooth_move(Page(w), 0, 0, 30, 30))
        self.assertEqual(len(moves(w)), 30)


class TestCursorStateFlag(unittest.TestCase):
    def test_default_is_not_native(self):
        self.assertFalse(ch.CursorState().native_motion)
        self.assertFalse(ch.CursorState(5, 6).native_motion)

    def test_flag_kept_across_updates(self):
        c = ch.CursorState(1, 2, native_motion=True)
        c.update(3, 4)
        self.assertTrue(c.native_motion)
        self.assertEqual((c.x, c.y), (3, 4))


class TestHelpersForwardNative(unittest.TestCase):
    """Default steps (30) on purpose: a helper that forgets `native=cursor.native_motion`
    shows up as 30 moves instead of 1."""

    def test_smooth_click_one_move_then_down_up(self):
        w = World()
        run(w, ch.smooth_click(Page(w), 800, 500, ch.CursorState(100, 100, native_motion=True)))
        self.assertEqual(kinds(w), ["move", "down", "up"])
        self.assertEqual(moves(w), [(800, 500)])

    def test_smooth_click_non_native_unchanged(self):
        w = World()
        run(w, ch.smooth_click(Page(w), 800, 500, ch.CursorState(100, 100)))
        self.assertEqual(kinds(w), ["move"] * 30 + ["down", "up"])

    def _far_click(self, native):
        w = World(mouse_pos=(W / 2, 20.0))          # over the navbar: wheel needs a relocation first
        far = w.add(El("far", top=3000, height=40))
        cur = ch.CursorState(W / 2, 20.0, native_motion=native)
        run(w, ch.click_box(Page(w), Locator(w, far), cur))
        return w

    def test_click_box_relocation_and_click_are_one_move_each(self):
        w = self._far_click(native=True)
        self.assertEqual(w.clicked, ["far"])
        self.assertGreater(len(wheels(w)), 0)
        self.assertEqual(len(moves(w)), 2, f"relocation into the scroller + the click point: {moves(w)}")
        rel = moves(w)[0]
        self.assertGreater(rel[1], VIS_TOP, "relocated into the content scroller before the first wheel")
        self.assertEqual(kinds(w).index("move"), 0)
        self.assertLess(kinds(w).index("move"), kinds(w).index("wheel"))

    def test_click_box_non_native_still_eases_both_moves(self):
        w = self._far_click(native=False)
        self.assertEqual(w.clicked, ["far"])
        self.assertEqual(len(moves(w)), 60)

    def test_point_at_one_move_beside_no_click(self):
        w = World()
        val = w.add(El("val", top=300, height=34, x=600, width=120))
        cur = ch.CursorState(W / 2, 600.0, native_motion=True)
        run(w, ch.point_at(Page(w), Locator(w, val), cur, hold=0))
        r = w.rect(val)
        self.assertEqual(moves(w), [(r["x"] + r["width"] + ch.POINT_GAP, r["y"] + r["height"] / 2)])
        self.assertNotIn("down", kinds(w))

    def test_scroll_through_one_move_to_centre(self):
        w = World(max_scroll=240)
        cur = ch.CursorState(10, 10, native_motion=True)
        run(w, ch.scroll_through(Page(w), cur, pause=0, hold=0))
        self.assertEqual(moves(w), [(W / 2, H / 2)])
        self.assertGreater(len(wheels(w)), 0)

    def test_click_text_and_type_into_one_move_each(self):
        w = World()
        w.add(El("ok", top=300, height=30, x=200, text="Confirm order"))
        field = w.add(El("name", top=500, height=34, x=800))
        cur = ch.CursorState(W / 2, 600.0, native_motion=True)
        page = Page(w)
        run(w, ch.click_text(page, "Confirm order", cur))
        run(w, ch.type_into(page, Locator(w, field), "new", cur))
        self.assertEqual(w.clicked, ["ok", "name"])
        self.assertEqual(len(moves(w)), 2)
        # lowercase "a": stealth Firefox reads "ControlOrMeta+A" as Ctrl+Shift+A — "old"+"new" = "oldnew"
        self.assertIn(("press", "ControlOrMeta+a"), w.events)


if __name__ == "__main__":
    unittest.main()
