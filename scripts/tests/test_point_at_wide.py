#!/usr/bin/env python3
"""point_at on a WIDE element — no browser.

Regression: point_at on a full-width `.o_data_row` (or any
container wider than half the page) parked the cursor beside the ELEMENT, and with no room on the right point_beside fell back to
its left edge (clamped to x = gap/2 = 8) — pointing at nothing. Elements wider than half the page now
point beside the first visible text inside them (`_FIRST_TEXT_BOX_JS`, evaluated on the locator);
no text → the old element box.

Reuses the page twin of test_click_helpers_scroll.py (World / Page / Locator / El). The twin locator
gains `evaluate()`, answering `_FIRST_TEXT_BOX_JS` with a configured text box (offset relative to
the element, like the JS). The JS itself runs only in test_click_helpers_real_chrome.py
(TestPointAtWideElement).

Run: python3 shared/skills/record-demo-video/scripts/tests/test_point_at_wide.py
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("click_helpers_wide_twin", HERE / "test_click_helpers_scroll.py")
assert _spec and _spec.loader
tw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tw)
ch = tw.ch
W, H, VIS_TOP = tw.W, tw.H, tw.VIS_TOP
World, El, Page, run, moves = tw.World, tw.El, tw.Page, tw.run, tw.moves
GAP = ch.POINT_GAP


class TextLocator(tw.Locator):
    """Twin locator that also answers `_FIRST_TEXT_BOX_JS` (dx/dy relative to the element's rect)."""

    def __init__(self, w, el, text_box):
        super().__init__(w, el)
        self.text_box, self.evaluated = text_box, []

    async def evaluate(self, js, *args):
        if js is not ch._FIRST_TEXT_BOX_JS:
            raise AssertionError(f"unexpected locator.evaluate: {js[:60]!r}")
        self.evaluated.append(js)
        return self.text_box


def kinds(w):
    return [e[0] for e in w.events if e[0] in ("move", "down", "up")]


class TestPointAtWideElement(unittest.TestCase):
    def point(self, el_kw, text_box, w=None, **kw):
        w = w or World()
        el = w.add(El("row", **el_kw))
        loc = TextLocator(w, el, text_box)
        cur = ch.CursorState(W / 2, 600.0, native_motion=True)        # native: exactly one move to the target
        run(w, ch.point_at(Page(w), loc, cur, hold=0, **kw))
        return w, el, loc, cur

    def test_full_width_row_points_beside_its_first_text_fix(self):
        tb = {"dx": 600, "dy": 7, "width": 90, "height": 18}
        w, el, loc, cur = self.point(dict(top=300, height=32, x=0, width=W), tb)
        r = w.rect(el)
        want = (r["x"] + 600 + 90 + GAP, r["y"] + 7 + 9)
        self.assertEqual(len(loc.evaluated), 1)
        self.assertEqual(moves(w), [want])
        self.assertEqual((cur.x, cur.y), want)
        self.assertGreater(cur.x, 100, "not parked at the left edge (x = gap/2) any more")
        self.assertNotIn("down", kinds(w), "point_at never clicks")

    def test_offset_is_relative_to_the_element_not_the_page(self):
        tb = {"dx": 250, "dy": 4, "width": 60, "height": 20}
        w, el, _, cur = self.point(dict(top=300, height=32, x=150, width=1500), tb)
        r = w.rect(el)
        self.assertEqual((cur.x, cur.y), (150 + 250 + 60 + GAP, r["y"] + 4 + 10))

    def test_text_near_the_right_edge_points_left_of_the_text(self):
        tb = {"dx": W - 60, "dy": 6, "width": 50, "height": 20}             # text ends 10 px from the edge
        w, el, _, cur = self.point(dict(top=300, height=32, x=0, width=W), tb)
        self.assertEqual(cur.x, W - 60 - GAP)
        self.assertEqual(cur.y, w.rect(el)["y"] + 6 + 10)

    def test_no_visible_text_falls_back_to_the_element_box(self):
        w, el, loc, cur = self.point(dict(top=300, height=32, x=0, width=W), None)
        r = w.rect(el)
        self.assertEqual(len(loc.evaluated), 1)
        self.assertEqual((cur.x, cur.y), (GAP / 2, r["y"] + 16), "old behaviour (point_beside of the element)")

    def test_empty_dict_counts_as_no_text(self):
        w, el, _, cur = self.point(dict(top=300, height=32, x=0, width=W), {})
        self.assertEqual(cur.x, GAP / 2)

    def test_half_width_or_narrower_is_unchanged_and_never_evaluates(self):
        for width in (120, W // 2):
            with self.subTest(width=width):
                w, el, loc, cur = self.point(dict(top=300, height=34, x=100, width=width),
                                             {"dx": 5, "dy": 5, "width": 5, "height": 5})
                r = w.rect(el)
                self.assertEqual(loc.evaluated, [], "narrow elements keep the plain element box")
                self.assertEqual((cur.x, cur.y), (100 + width + GAP, r["y"] + 17))

    def test_just_over_half_width_uses_the_text(self):
        w, el, loc, cur = self.point(dict(top=300, height=34, x=0, width=W // 2 + 1),
                                     {"dx": 300, "dy": 8, "width": 40, "height": 18})
        self.assertEqual(len(loc.evaluated), 1)
        self.assertEqual(cur.x, 300 + 40 + GAP)

    def test_width_rule_reads_inner_width_when_viewport_size_is_none(self):
        # recorder.py launches with no_viewport=True → page.viewport_size is None → innerWidth.
        w = World(viewport_size=False)
        w2, el, loc, cur = self.point(dict(top=300, height=32, x=0, width=W), {"dx": 600, "dy": 7, "width": 90, "height": 18}, w=w)
        self.assertIn(("inner_dims",), w2.events)
        self.assertEqual(len(loc.evaluated), 1)
        self.assertEqual(cur.x, 600 + 90 + GAP)

    def test_row_below_the_fold_is_scrolled_first_and_the_text_box_follows(self):
        w = World()
        tb = {"dx": 600, "dy": 7, "width": 90, "height": 18}
        w2, el, _, cur = self.point(dict(top=2400, height=32, x=0, width=W), tb, w=w)
        r = w2.rect(el)
        self.assertGreater(w2.scroll, 0, "scrolled into view first")
        self.assertGreaterEqual(r["y"], VIS_TOP)
        self.assertEqual((cur.x, cur.y), (600 + 90 + GAP, r["y"] + 7 + 9), "post-scroll position")

    def test_scroll_false_points_without_scrolling(self):
        tb = {"dx": 600, "dy": 7, "width": 90, "height": 18}
        w, el, _, cur = self.point(dict(top=300, height=32, x=0, width=W), tb, scroll=False)
        self.assertEqual(w.scroll, 0)
        self.assertEqual(cur.x, 600 + 90 + GAP)

    def test_in_frame_offsets_are_scaled_by_box_over_element_size(self):
        # Regression: inside a CSS-scaled iframe the JS offsets are
        # in frame px while bounding_box() is in page px — adding them unscaled put the cursor at 1305.8
        # instead of ≈711 (scale 0.5). ew/eh = the element's in-frame size → sx = box.width / ew.
        tb = {"dx": 1200, "dy": 14, "width": 180, "height": 36, "ew": W * 2, "eh": 64}   # page box = half
        w, el, _, cur = self.point(dict(top=300, height=32, x=0, width=W), tb)
        r = w.rect(el)
        self.assertEqual((cur.x, cur.y), (600 + 90 + GAP, r["y"] + 7 + 9))

    def test_unit_scale_when_box_matches_the_element(self):
        tb = {"dx": 600, "dy": 7, "width": 90, "height": 18, "ew": W, "eh": 32}
        w, el, _, cur = self.point(dict(top=300, height=32, x=0, width=W), tb)
        self.assertEqual((cur.x, cur.y), (600 + 90 + GAP, w.rect(el)["y"] + 7 + 9))

    def test_custom_gap_applies_beside_the_text(self):
        tb = {"dx": 600, "dy": 7, "width": 90, "height": 18}
        _, _, _, cur = self.point(dict(top=300, height=32, x=0, width=W), tb, gap=30)
        self.assertEqual(cur.x, 600 + 90 + 30)


if __name__ == "__main__":
    unittest.main()
