#!/usr/bin/env python3
"""Test phần PURE của click_helpers.py (ease_out_cubic) — không cần Playwright/browser thật.

Chạy: python3 shared/skills/record-demo-video/scripts/tests/test_click_helpers.py
"""
from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "click_helpers.py"

spec = importlib.util.spec_from_file_location("click_helpers_mod", SCRIPT)
assert spec and spec.loader
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)


class TestEaseOutCubic(unittest.TestCase):
    def test_boundary_values(self):
        self.assertEqual(ch.ease_out_cubic(0.0), 0.0)
        self.assertAlmostEqual(ch.ease_out_cubic(1.0), 1.0)

    def test_monotonic_increasing(self):
        samples = [ch.ease_out_cubic(t / 10) for t in range(11)]
        for a, b in zip(samples, samples[1:]):
            self.assertLessEqual(a, b, "ease-out phải tăng đơn điệu theo t")

    def test_faster_at_start_than_linear(self):
        # ease-out-cubic: nhanh lúc đầu, chậm dần cuối — tại t=0.3 phải tiến bộ NHIỀU hơn linear.
        self.assertGreater(ch.ease_out_cubic(0.3), 0.3)

    def test_slower_near_end_than_linear(self):
        self.assertLess(ch.ease_out_cubic(0.9) - ch.ease_out_cubic(0.8), 0.1)


DOT_RADIUS = 9      # cursor_overlay.js DOT_SIZE 18 px


def _box(x, y, w, h):
    return {"x": x, "y": y, "width": w, "height": h}


class TestPointBeside(unittest.TestCase):
    """point_beside parks the arrow NEXT TO an element, never on its centre (in a real take
    the cursor hid the very value the narration explained, "677.5")."""

    def test_right_of_element_at_vertical_centre(self):
        x, y = ch.point_beside(_box(100, 200, 80, 30), 1920)
        self.assertEqual((x, y), (100 + 80 + ch.POINT_GAP, 215))

    def test_never_the_centre_and_dot_clears_the_element_with_default_gap(self):
        b = _box(600, 300, 120, 34)
        x, y = ch.point_beside(b, 1920)
        self.assertNotEqual((x, y), (660, 317), "the dot on the centre covers the value")
        right = b["x"] + b["width"]
        self.assertGreaterEqual(x - DOT_RADIUS, right, "the 18 px dot must not overlap the element")
        self.assertGreater(x, right)

    def test_near_right_edge_goes_left(self):
        b = _box(1850, 400, 60, 20)
        x, y = ch.point_beside(b, 1920)
        self.assertEqual((x, y), (1850 - ch.POINT_GAP, 410))
        self.assertLessEqual(x + DOT_RADIUS, b["x"], "left side: the dot still clears the element")

    def test_right_side_boundary_is_half_a_gap_from_the_screen_edge(self):
        gap = ch.POINT_GAP
        fits = _box(1000, 0, 1920 - gap / 2 - gap - 1000, 10)          # right point == vw - gap/2
        self.assertEqual(ch.point_beside(fits, 1920)[0], 1920 - gap / 2)
        no_fit = _box(1001, 0, 1920 - gap / 2 - gap - 1000, 10)
        self.assertEqual(ch.point_beside(no_fit, 1920)[0], 1001 - gap)

    def test_very_wide_element_stays_on_screen(self):
        # No room on either side: clamp to half a gap from the left edge (still visible).
        x, y = ch.point_beside(_box(0, 50, 1920, 40), 1920)
        self.assertEqual((x, y), (ch.POINT_GAP / 2, 70))
        self.assertGreater(x, 0)

    def test_custom_gap_is_honoured_both_sides(self):
        self.assertEqual(ch.point_beside(_box(100, 0, 50, 10), 1920, gap=30), (180, 5))
        self.assertEqual(ch.point_beside(_box(1880, 0, 30, 10), 1920, gap=30), (1850, 5))

    def test_fractional_box_y_centre(self):
        _, y = ch.point_beside(_box(10.5, 101.25, 20, 33.5), 800)
        self.assertAlmostEqual(y, 118.0)


class TestCursorState(unittest.TestCase):
    def test_update_tracks_last_position(self):
        cursor = ch.CursorState(0, 0)
        cursor.update(100, 200)
        self.assertEqual((cursor.x, cursor.y), (100, 200))


if __name__ == "__main__":
    unittest.main()
