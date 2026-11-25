#!/usr/bin/env python3
"""Real-Chromium tests for cursor_overlay.js (arrow cursor + click ripple).

Only a real browser executes the injected JS, so this is the only place the arrow can be checked.
What matters for a user manual: the arrow TIP sits exactly on the mouse position (click_helpers.py computes
every target for that point, not for the shape's bounding box), a click shows a ripple that cleans itself up,
and the overlay never swallows a real click.

The tip is read from the SVG path geometry (`getPointAtLength(0)` through `getScreenCTM()`), never from the
bounding box, and the body is checked with `isPointInFill` on both sides of the tip.
Each test uses its OWN browser context, `add_init_script` and `page.goto("data:…")` — NOT `page.set_content`,
which wipes the DOM the init script just built.

The overlay source is read at import: a missing file is a crash, not a skip. Only a missing playwright
package or a Chromium that cannot launch prints one `SKIP-FILE: <reason>` line and exits 0 (per-file runner
protocol). Chromium is launched ONCE per file.

Run: python tests/test_cursor_overlay_real_chrome.py   (or scripts/tests/…, from the repo root)
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERLAY_SRC = (HERE.parent / "cursor_overlay.js").read_text(encoding="utf-8")   # a missing file must crash

VW, VH = 1920, 1080
LOOP: asyncio.AbstractEventLoop | None = None
PW = None
BROWSER = None

PAGE_BUTTON = ("<html><body style='margin:0;background:#fff'>"
               "<button id=b style='position:absolute;left:340px;top:270px;width:120px;height:60px'"
               " onclick=\"window.__clicks=(window.__clicks||0)+1\">GO</button></body></html>")
PAGE_PLAIN = "<html><body style='margin:0;background:#fff'><p>second page</p></body></html>"


def data_url(html: str) -> str:
    return "data:text/html;charset=utf-8," + urllib.parse.quote(html)


TIP_JS = """() => {
  const path = document.querySelector('#__demo_cursor__ path');
  const m = path.getScreenCTM();
  const p = path.getPointAtLength(0);
  const s = new DOMPoint(p.x, p.y).matrixTransform(m);
  return [s.x, s.y];
}"""

# is the screen point (x, y) inside the arrow's FILL (path coordinates via the inverse CTM)?
IN_FILL_JS = """([x, y]) => {
  const path = document.querySelector('#__demo_cursor__ path');
  const local = new DOMPoint(x, y).matrixTransform(path.getScreenCTM().inverse());
  return path.isPointInFill(local);
}"""


# Headless Chromium may emit one synthetic mousemove at (0,0) after the first load; under machine load it can land
# before an assertion. Counting the events lets a test state the rule exactly: hidden iff no mousemove has happened.
MOVE_COUNTER_JS = "window.__moves = 0; addEventListener('mousemove', () => { window.__moves++; }, {capture: true, passive: true});"

PAGE_CSS_RESET = ("<html><head><style>svg { max-width: 100% !important; height: auto !important; width: 0 !important;"
                  " margin: 30px !important } * { box-sizing: border-box }"
                  "</style></head><body style='margin:0;background:#fff'><p>host page with an svg reset</p></body></html>")

DISPLAY_JS = "() => getComputedStyle(document.getElementById('__demo_cursor__')).display"


class OverlayCase(unittest.TestCase):
    def setUp(self):
        self.ctx = self.run_(BROWSER.new_context(viewport={"width": VW, "height": VH}))
        self.run_(self.ctx.add_init_script(script=MOVE_COUNTER_JS))   # BEFORE the overlay: counts every mousemove
        self.run_(self.ctx.add_init_script(script=OVERLAY_SRC))
        self.page = self.run_(self.ctx.new_page())

    def tearDown(self):
        self.run_(self.ctx.close())

    @staticmethod
    def run_(coro):
        return LOOP.run_until_complete(coro)

    def open(self, html: str = PAGE_PLAIN):
        self.run_(self.page.goto(data_url(html), wait_until="load"))
        # On a tiny data: page the load event can fire before the overlay's setup() poll (10 ms) has mounted it
        # and registered its listeners; a real page is never that fast, so wait for the overlay here.
        self.run_(self.page.wait_for_selector("#__demo_cursor__", state="attached", timeout=5000))

    def move(self, x: float, y: float):
        self.run_(self.page.mouse.move(x, y))

    def ev(self, js: str, arg=None):
        return self.run_(self.page.evaluate(js, arg))


class TestArrow(OverlayCase):
    def test_hidden_until_the_first_real_mouse_move(self):
        self.open()
        # Invariant, independent of the synthetic (0,0) mousemove the bundled Chromium may send at load: the arrow is
        # visible ONLY after the overlay has positioned it (the one code path that shows it also sets the transform).
        # A stray, unpositioned arrow at the page corner would be display:block with an empty transform.
        display, transform = self.ev("""() => { const a = document.getElementById('__demo_cursor__');
                                               return [getComputedStyle(a).display, a.style.transform]; }""")
        if display == "block":
            self.assertNotEqual(transform, "", "visible but never positioned")
        self.move(100, 100)
        self.assertEqual(self.ev(DISPLAY_JS), "block")
        tip_x, tip_y = self.ev(TIP_JS)
        self.assertAlmostEqual(tip_x, 100, delta=0.6)
        self.assertAlmostEqual(tip_y, 100, delta=0.6)

    def test_a_host_page_svg_reset_cannot_shrink_the_arrow(self):
        self.open(PAGE_CSS_RESET)
        self.move(300, 200)
        w, h = self.ev("""() => { const r = document.querySelector('#__demo_cursor__ svg').getBoundingClientRect();
                                   return [r.width, r.height]; }""")
        self.assertGreaterEqual(w, 18)
        self.assertGreaterEqual(h, 24)
        tip_x, tip_y = self.ev(TIP_JS)          # and a hostile host margin must not move the tip off the pointer
        self.assertAlmostEqual(tip_x, 300, delta=0.6)
        self.assertAlmostEqual(tip_y, 200, delta=0.6)

    def test_the_ripple_is_drawn_below_the_arrow(self):
        self.open()
        self.move(400, 300)
        self.run_(self.page.mouse.down())
        try:
            z_ripple = self.ev("() => +getComputedStyle(document.querySelector('.__demo_ripple__')).zIndex")
            z_arrow = self.ev("() => +getComputedStyle(document.getElementById('__demo_cursor__')).zIndex")
        finally:
            self.run_(self.page.mouse.up())
        self.assertLess(z_ripple, z_arrow)

    def test_the_arrow_keeps_its_tip_on_a_high_dpi_screen(self):
        ctx = self.run_(BROWSER.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=2))
        try:
            self.run_(ctx.add_init_script(script=OVERLAY_SRC))
            page = self.run_(ctx.new_page())
            self.run_(page.goto(data_url(PAGE_PLAIN), wait_until="load"))
            self.run_(page.wait_for_selector("#__demo_cursor__", state="attached", timeout=5000))
            self.run_(page.mouse.move(333, 222))
            tip_x, tip_y = self.run_(page.evaluate(TIP_JS))
        finally:
            self.run_(ctx.close())
        self.assertAlmostEqual(tip_x, 333, delta=0.6)
        self.assertAlmostEqual(tip_y, 222, delta=0.6)

    def test_the_tip_is_exactly_on_the_mouse_position(self):
        self.open()
        for x, y in ((10, 10), (300, 200), (1000, 500), (1900, 1070)):
            with self.subTest(at=(x, y)):
                self.move(x, y)
                tip_x, tip_y = self.ev(TIP_JS)
                self.assertAlmostEqual(tip_x, x, delta=0.6)
                self.assertAlmostEqual(tip_y, y, delta=0.6)

    def test_the_body_lies_down_and_right_of_the_tip_not_around_it(self):
        self.open()
        self.move(800, 400)
        tip_x, tip_y = self.ev(TIP_JS)
        self.assertTrue(self.ev(IN_FILL_JS, [tip_x + 2, tip_y + 9]), "inside the arrow body just below the tip")
        for dx, dy in ((-3, -3), (-4, 8), (3, -6)):
            with self.subTest(offset=(dx, dy)):
                self.assertFalse(self.ev(IN_FILL_JS, [tip_x + dx, tip_y + dy]), "must be empty: the tip is a corner")

    def test_the_arrow_is_big_enough_to_see_in_a_1080p_video(self):
        self.open()
        self.move(800, 400)
        h = self.ev("() => document.querySelector('#__demo_cursor__ svg').getBoundingClientRect().height")
        self.assertGreaterEqual(h, 24)
        self.assertLessEqual(h, 60)

    def test_no_red_dot_is_left(self):
        self.open()
        self.move(800, 400)
        self.assertFalse(self.ev("() => !!document.getElementById('__demo_cursor_dot__')"))

    def test_the_os_cursor_is_hidden_inside_the_page(self):
        self.open()
        self.assertEqual(self.ev("() => getComputedStyle(document.body).cursor"), "none")

    def test_the_arrow_survives_a_second_navigation(self):
        self.open()
        self.open(PAGE_BUTTON)
        self.move(500, 500)
        self.assertEqual(self.ev("() => getComputedStyle(document.getElementById('__demo_cursor__')).display"), "block")


class TestRipple(OverlayCase):
    COUNT_JS = "() => document.querySelectorAll('.__demo_ripple__').length"

    def test_a_press_shows_a_ripple_centred_on_the_pointer_then_removes_it(self):
        self.open()
        self.move(400, 300)
        self.run_(self.page.mouse.down())
        try:
            self.assertEqual(self.ev(self.COUNT_JS), 1)
            cx, cy = self.ev("""() => { const r = document.querySelector('.__demo_ripple__').getBoundingClientRect();
                                       return [r.left + r.width / 2, r.top + r.height / 2]; }""")
            self.assertAlmostEqual(cx, 400, delta=1.0)
            self.assertAlmostEqual(cy, 300, delta=1.0)
        finally:
            self.run_(self.page.mouse.up())
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and self.ev(self.COUNT_JS):
            self.run_(asyncio.sleep(0.1))
        self.assertEqual(self.ev(self.COUNT_JS), 0, "the ripple must clean itself up")

    def test_overlay_never_swallows_a_real_click(self):
        self.open(PAGE_BUTTON)
        self.move(400, 300)
        self.run_(self.page.mouse.down())
        self.run_(self.page.mouse.up())
        self.assertEqual(self.ev("() => window.__clicks"), 1)

    def test_ripple_and_arrow_do_not_intercept_pointer_events(self):
        self.open(PAGE_BUTTON)
        self.move(400, 300)
        self.run_(self.page.mouse.down())
        try:
            self.assertEqual(self.ev("() => document.elementFromPoint(400, 300).id"), "b")
        finally:
            self.run_(self.page.mouse.up())


async def _start():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    errors = []
    for exe in (None, "/usr/bin/google-chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        # Check the bundled binary too: a failed launch() leaves empty playwright temp dirs behind.
        if not os.path.exists(exe if exe is not None else pw.chromium.executable_path):
            errors.append(f"{exe or 'bundled chromium'}: not installed")
            continue
        try:
            kw = {"executable_path": exe} if exe else {}
            return pw, await pw.chromium.launch(headless=True, **kw)
        except Exception as e:  # noqa: BLE001 — any launch failure → try the next binary
            errors.append(f"{exe or 'bundled chromium'}: {type(e).__name__}: {str(e).splitlines()[0][:80]}")
    await pw.stop()
    raise RuntimeError("; ".join(errors) or "no Chromium candidate")


def main() -> int:
    global LOOP, PW, BROWSER
    try:
        import playwright.async_api  # noqa: F401
    except Exception as e:  # noqa: BLE001
        print(f"SKIP-FILE: playwright not importable ({type(e).__name__}: {e})"[:300])
        return 0
    LOOP = asyncio.new_event_loop()
    try:
        PW, BROWSER = LOOP.run_until_complete(_start())
    except Exception as e:  # noqa: BLE001
        LOOP.close()
        print(f"SKIP-FILE: no launchable Chromium ({e})"[:300])
        return 0
    try:
        prog = unittest.main(exit=False)
    finally:
        LOOP.run_until_complete(BROWSER.close())
        LOOP.run_until_complete(PW.stop())
        LOOP.close()
    return 0 if prog.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
