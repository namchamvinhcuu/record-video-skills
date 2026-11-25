#!/usr/bin/env python3
"""Tests for the scrolling / typing / waiting helpers of click_helpers.py — no browser.

The fake models an Odoo-17-like page geometry:
- a page of innerWidth x innerHeight (default 1920 x 992 — the real inner size with
  recorder.py's no_viewport window), whose `viewport_size` may be None;
- a fixed panel (navbar + control panel) above `vis_top` (default 104 px): content under it
  is clipped and a click there hits the panel;
- one content scroller (`.o_content`) from `vis_top` to the bottom; `mouse.wheel` scrolls it
  only while the pointer is over it;
- "fixed" elements (navbar menu, control-panel buttons) that never move;
- optional covers (sticky header / modal backdrop) that win the hit test;
- dropdown items (`closes_on_scroll`) that disappear on ANY content scroll, like Odoo 17's
  AutoComplete cancelling itself on window scroll.
`click_down` records what the pointer hit-tests at the mouse-down point: a wrong click is
visible as a wrong name in `World.clicked`.

The fake speaks both the round-1 protocol (locator.bounding_box / page.evaluate of
_FIND_LEAF_TEXT_JS returning a point) and the current one (element handles evaluating
_TARGET_STATE_JS, page.evaluate_handle), so the regression class `TestReviewFindingsRegression`
can be run against a copy of the round-1 module to show it red.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_click_helpers_scroll.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "click_helpers.py"

spec = importlib.util.spec_from_file_location("click_helpers_scroll_mod", SCRIPT)
assert spec and spec.loader
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)

W, H = 1920, 992          # innerWidth / innerHeight
VIS_TOP = 104             # .o_content top (navbar 46 + control panel 58)


class El:
    def __init__(self, name, top, height=40, x=500, width=200, layer="content", text=None,
                 closes_on_scroll=False, clip=None, clip_is_cb=False):
        # layer "content": `top` is in scroller-content coordinates (viewport y = vis_top + top - scroll)
        # layer "fixed":   `top` is in viewport coordinates and never moves (position: fixed)
        # clip: viewport (top, bottom) of an overflow ancestor (e.g. `.table-responsive`).
        #   A position:fixed element is NOT clipped by it unless that ancestor is its containing
        #   block (transform/filter/... → clip_is_cb=True) — the twin of _TARGET_STATE_JS's rule.
        self.name, self.top, self.height, self.x, self.width = name, top, height, x, width
        self.layer, self.text, self.closes_on_scroll = layer, text, closes_on_scroll
        self.clip, self.clip_is_cb = clip, clip_is_cb
        self.alive = True


class MainFrame:
    parent_frame = None


class ChildFrame:
    def __init__(self, parent_world, frame_el):
        self.parent_frame = MainFrame()
        self._pw, self._el = parent_world, frame_el

    async def frame_element(self):
        self._pw.events.append(("frame_element",))
        return Handle(self._pw, self._el)


class World:
    def __init__(self, scroll=0, max_scroll=100_000, inner_h=H, vis_top=VIS_TOP, viewport_size=True,
                 wheel_enabled=True, force_noop=False, mouse_pos=(W / 2, 600.0), width=W, snap=False,
                 scale=1.0):
        self.scale = scale              # child frame only: CSS transform scale of its <iframe>
        self.scroll, self.max_scroll = scroll, max_scroll
        self.inner_h, self.vis_top, self.width = inner_h, vis_top, width
        self.viewport_size = viewport_size
        self.wheel_enabled, self.force_noop = wheel_enabled, force_noop
        self.snap = snap                # browsers snap scroll offsets to whole px (DPR 1)
        self.mouse_pos = mouse_pos
        self.els: list[El] = []
        self.covers: list[tuple[str, float, float]] = []   # (name, top, bottom) in viewport coords
        self.events: list = []
        self.sleeps: list = []
        self.clicked: list = []
        self.forced = 0
        self.disposed = 0
        self.find_args: list = []
        self.kill_on_find = False
        self.frames: list = []          # [(iframe El in this world, child World)]
        self.parent = None              # child world: (parent World, iframe El)
        self.frame = MainFrame()
        self.containing_block_rule = True   # False = round-2 twin (clip applies to fixed too)

    def add_frame(self, frame_el: El, child: "World") -> "World":
        self.add(frame_el)
        self.frames.append((frame_el, child))
        child.parent = (self, frame_el)
        child.frame = ChildFrame(self, frame_el)
        child.events = self.events      # one event log for the whole page
        child.clicked = self.clicked
        return child

    def to_main(self, x, y):
        """Frame-local (x, y) → MAIN-page coordinates, through every (possibly scaled) iframe."""
        if self.parent is None:
            return x, y
        pw, fe = self.parent
        return pw.to_main(fe.x + x * self.scale, pw.rect_top(fe) + y * self.scale)

    def cum_scale(self):
        return 1.0 if self.parent is None else self.scale * self.parent[0].cum_scale()

    def clip_applies(self, el):
        return el.clip is not None and (el.layer != "fixed" or el.clip_is_cb or not self.containing_block_rule)

    # geometry -------------------------------------------------------------
    def add(self, el: El) -> El:
        self.els.append(el)
        return el

    def rect_top(self, el):
        return self.vis_top + el.top - self.scroll if el.layer == "content" else el.top

    def rect(self, el):
        if not el.alive:
            return None
        return {"x": el.x, "y": self.rect_top(el), "width": el.width, "height": el.height}

    def hit(self, x, y):
        if not (0 <= x < self.width and 0 <= y < self.inner_h):
            return None
        for name, t, b in self.covers:
            if t <= y < b:
                return name
        ordered = ([e for e in self.els if e.layer == "fixed"]
                   + ([] if y < self.vis_top else
                      [e for e in self.els if e.layer == "content" and e.closes_on_scroll]
                      + [e for e in self.els if e.layer == "content" and not e.closes_on_scroll]))
        for e in ordered:
            if not e.alive:
                continue
            if self.clip_applies(e) and not (e.clip[0] <= y < e.clip[1]):
                continue                     # clipped away: not painted there
            t = self.rect_top(e)
            if e.x <= x < e.x + e.width and t <= y < t + e.height:
                return e.name
        return "panel" if y < self.vis_top else "background"

    def vis(self, el):
        vt, vb = (0, self.inner_h) if el.layer == "fixed" else (self.vis_top, self.inner_h)
        if self.clip_applies(el):
            vt, vb = max(vt, el.clip[0]), min(vb, el.clip[1])
        return vt, vb

    def box(self, el):
        return [0, 0, self.width, self.inner_h] if el.layer == "fixed" else [0, self.vis_top, self.width, self.inner_h]

    def in_scroller(self, x, y):
        return 0 <= x <= self.width and self.vis_top <= y <= self.inner_h

    def scroll_content(self, dy):
        new = max(0, min(self.max_scroll, self.scroll + dy))
        if self.snap:
            new = round(new)
        if new != self.scroll:
            self.scroll = new
            for e in self.els:
                if e.closes_on_scroll:
                    e.alive = False

    def centre(self, el, count=True):
        if count:
            self.forced += 1
        if self.force_noop or el.layer != "content" or not el.alive:
            return
        mid = (self.inner_h - self.vis_top) / 2
        self.scroll_content(el.top + el.height / 2 - mid - self.scroll)

    def jump(self, el):
        """Playwright scroll_into_view_if_needed: centre the target at EVERY frame level (the
        target's own rect, translated up the chain — not the iframe element's centre)."""
        self.events.append(("jump",))
        if not el.alive:
            return
        self.centre(el, count=False)
        w, top, h = self, self.rect_top(el), el.height
        while w.parent is not None:
            pw, fe = w.parent
            top, h = top * w.scale, h * w.scale       # into the parent's (scaled) units
            vt = pw.rect_top(fe) + top                 # target top in the parent's viewport
            if fe.layer == "content" and not pw.force_noop:
                pw.scroll_content(vt + h / 2 - (pw.vis_top + pw.inner_h) / 2)
            top = pw.rect_top(fe) + top
            w = pw

    def state(self, el):
        """Python twin of _TARGET_STATE_JS for this geometry (frame-local coordinates):
        containing-block clip rule, click point = middle of the VISIBLE part (fallback: centre
        clamped into the area), rLeft."""
        r = self.rect(el)
        if r is None or (r["width"] == 0 and r["height"] == 0):
            return None
        top, bottom = r["y"], r["y"] + r["height"]
        vt, vb = self.vis(el)

        def mid(a0, a1, lo, hi):
            s, e = max(a0, lo), min(a1, hi)
            return (s + e) / 2 if e > s else min(max((a0 + a1) / 2, lo + 1), hi - 1)

        x = mid(r["x"], r["x"] + r["width"], 0, self.width)
        y = mid(top, bottom, vt, vb)
        return {"top": top, "bottom": bottom, "rLeft": r["x"], "rWidth": r["width"], "rHeight": r["height"],
                "visTop": vt, "visBottom": vb,
                "x": x, "y": y, "hit": self.hit(x, y) == el.name, "box": self.box(el)}

    def find(self, text, case_sensitive):
        norm = (lambda s: s) if case_sensitive else (lambda s: s.lower())
        for e in self.els:
            if e.alive and e.text is not None and norm(text) in norm(e.text):
                return e
        return None


class Handle:
    def __init__(self, w: World, el: El):
        self.w, self.el = w, el

    async def evaluate(self, js, *args):
        w, el = self.w, self.el
        if js is ch._TARGET_STATE_JS:
            return w.state(el)
        if js is getattr(ch, "_HIT_AT_JS", None):
            # point (in THIS frame's coordinates) inside this frame's viewport AND hits el
            x, y = args[0]
            if not (0 <= x < w.width and 0 <= y < w.inner_h) or not el.alive:
                return False
            return w.hit(x, y) == el.name
        if "getBoundingClientRect(); return [r.left, r.top" in js:
            r = w.rect(el)                           # in-frame rect (unscaled frame units)
            if "r.width" in js:
                return [r["x"], r["y"], r["width"], r["height"]]
            return [r["x"], r["y"]]
        if "scrollIntoView" in js:
            w.centre(el)
            return None
        raise AssertionError(f"unexpected handle.evaluate: {js[:60]!r}")

    async def scroll_into_view_if_needed(self):
        self.w.jump(self.el)

    def as_element(self):
        return self

    async def owner_frame(self):
        return self.w.frame

    async def bounding_box(self):
        """Playwright semantics: MAIN-page coordinates, also for an element inside an iframe."""
        r = self.w.rect(self.el)
        if r is None:
            return None
        mx, my = self.w.to_main(r["x"], r["y"])
        s = self.w.cum_scale()
        return {"x": mx, "y": my, "width": r["width"] * s, "height": r["height"] * s}

    async def dispose(self):
        self.w.disposed += 1


class NullHandle:
    def __init__(self, w: World):
        self.w = w

    def as_element(self):
        return None

    async def dispose(self):
        self.w.disposed += 1


class Locator:
    def __init__(self, w: World, el: El | None):
        self.w, self.el = w, el

    async def element_handle(self):
        return None if self.el is None or not self.el.alive else Handle(self.w, self.el)

    # round-1 protocol
    async def bounding_box(self):
        return None if self.el is None else self.w.rect(self.el)

    async def scroll_into_view_if_needed(self):
        self.w.centre(self.el)


class Mouse:
    def __init__(self, w: World):
        self.w = w

    async def move(self, x, y):
        self.w.mouse_pos = (x, y)
        self.w.events.append(("move", x, y))

    def _frame_at(self, x, y):
        if not (0 <= x < self.w.width and 0 <= y < self.w.inner_h):
            return None, None                # off-screen pointer: hits nothing
        top_hit = self.w.hit(x, y)           # panel / covers above the iframe win
        for fe, child in self.w.frames:
            r = self.w.rect(fe)
            if top_hit == fe.name and r:
                return child, ((x - r["x"]) / child.scale, (y - r["y"]) / child.scale)
        return None, None

    async def wheel(self, dx, dy):
        self.w.events.append(("wheel", dy, self.w.mouse_pos))
        if not self.w.wheel_enabled:
            return
        child, local = self._frame_at(*self.w.mouse_pos)
        if child is not None and child.in_scroller(*local):
            before = child.scroll
            child.scroll_content(dy)
            if child.scroll != before:
                return                       # the iframe consumed the wheel; else it chains out
        if self.w.in_scroller(*self.w.mouse_pos):
            self.w.scroll_content(dy)

    async def down(self):
        x, y = self.w.mouse_pos
        self.w.events.append(("down", x, y))
        child, local = self._frame_at(x, y)
        if child is not None:
            self.w.clicked.append("frame:" + str(child.hit(*local)))
        else:
            self.w.clicked.append(self.w.hit(x, y))

    async def up(self):
        self.w.events.append(("up",))


class Keyboard:
    def __init__(self, w: World):
        self.w = w

    async def type(self, text, delay=None):
        self.w.events.append(("type", text, delay))

    async def press(self, key):
        self.w.events.append(("press", key))


class Page:
    def __init__(self, w: World):
        self.w = w
        self.mouse, self.keyboard = Mouse(w), Keyboard(w)

    @property
    def viewport_size(self):
        return {"width": W, "height": self.w.inner_h} if self.w.viewport_size else None

    def scroll_positions(self, js):
        return [self.w.scroll, 0] if "pos.push" in js else self.w.scroll

    async def evaluate(self, js, args=None):
        w = self.w
        if js is ch._SCROLL_POS_JS:
            w.events.append(("scrollpos", list(args)))
            return self.scroll_positions(js)
        if js is ch._FIND_LEAF_TEXT_JS:          # round-1 protocol: returns a point dict
            w.find_args.append(list(args))
            el = w.find(args[0], args[1])
            if el is None:
                return None
            if len(args) > 2 and args[2]:
                w.centre(el)
            r = w.rect(el)
            return {"x": r["x"] + r["width"] / 2, "y": r["y"] + r["height"] / 2,
                    "top": r["y"], "bottom": r["y"] + r["height"]}
        if "innerHeight" in js:
            w.events.append(("inner_dims",))
            return [W, w.inner_h]
        raise AssertionError(f"unexpected page.evaluate: {js[:60]!r}")

    async def evaluate_handle(self, js, args):
        assert js is ch._FIND_LEAF_TEXT_JS
        self.w.find_args.append(list(args))
        el = self.w.find(args[0], args[1])
        if el is None:
            return NullHandle(self.w)
        if self.w.kill_on_find:
            el.alive = False
        return Handle(self.w, el)

    async def wait_for_load_state(self, state):
        self.w.events.append(("load_state", state))

    async def wait_for_selector(self, selector, state=None, timeout=None):
        self.w.events.append(("selector", selector, state, timeout))


def run(w: World, coro):
    async def fake_sleep(s, *a, **kw):
        w.sleeps.append(s)

    with mock.patch.object(ch.asyncio, "sleep", fake_sleep):
        return asyncio.run(coro)


def wheels(w):
    return [e[1] for e in w.events if e[0] == "wheel"]


def moves(w):
    return [(e[1], e[2]) for e in w.events if e[0] == "move"]


def fast(**kw):
    return {"steps": 2, "duration": 0, **kw}


# ---------------------------------------------------------------------------------------------
# Regressions for the 4 🟠 of the round-1 review. Only round-1-compatible public API here
# (click_box / click_text / type_into / scroll_through), so this class also runs on the round-1
# module and shows RED there.
# ---------------------------------------------------------------------------------------------
class TestReviewFindingsRegression(unittest.TestCase):
    # 🟠 1 — scrolling UP parked a short target at 80 px, under Odoo 17's control panel
    # (.o_content starts at 104 px): the click landed on the panel, silently.
    def test_regression_scroll_up_click_hits_target_not_control_panel(self):
        w = World(scroll=3000)
        target = w.add(El("partner", top=500, height=34, x=800))
        run(w, ch.click_box(Page(w), Locator(w, target), ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, ["partner"])
        self.assertTrue(all(d < 0 for d in wheels(w)))

    def test_regression_scroll_up_type_into_clicks_the_field(self):
        w = World(scroll=3000)
        field = w.add(El("partner", top=500, height=34, x=800))
        run(w, ch.type_into(Page(w), Locator(w, field), "Azure", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, ["partner"])

    def test_regression_scroll_up_click_text_hits_target(self):
        w = World(scroll=3000)
        w.add(El("confirm", top=400, height=34, x=200, text="Confirm order"))
        run(w, ch.click_text(Page(w), "Confirm order", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, ["confirm"])

    # 🟠 3 — a fixed / already-visible target cost 2 wheel notches, a detour through the frame
    # centre and a jitter of the content behind it.
    def test_regression_fixed_navbar_target_no_wheel_no_centre_glide(self):
        w = World(scroll=600, mouse_pos=(1500.0, 800.0))
        menu = w.add(El("menu", top=10, height=30, x=200, width=100, layer="fixed"))
        run(w, ch.click_box(Page(w), Locator(w, menu), ch.CursorState(1500, 800), **fast()))
        self.assertEqual(wheels(w), [])
        self.assertEqual(w.scroll, 600, "content behind must not jitter")
        self.assertFalse([p for p in moves(w) if abs(p[0] - W / 2) < 1 and abs(p[1] - H / 2) < 1],
                         "no glide through the frame centre")
        self.assertEqual(w.clicked, ["menu"])

    def test_regression_visible_content_target_near_bottom_edge_no_wheel(self):
        w = World(scroll=0)
        btn = w.add(El("save", top=950 - VIS_TOP, height=30))       # viewport 950..980 of 992
        run(w, ch.click_box(Page(w), Locator(w, btn), ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(wheels(w), [])
        self.assertEqual(w.clicked, ["save"])

    # 🟠 4 — Many2one: the wheel scrolled .o_content, the AutoComplete dropdown closed, and the
    # helper then clicked ANOTHER node with the same text (or crashed).
    def test_regression_visible_dropdown_item_clicked_without_wheel(self):
        w = World(scroll=1000)
        item = w.add(El("item", top=900 - VIS_TOP + 1000, height=30, text="Azure Interior",
                        closes_on_scroll=True))
        w.add(El("other", top=3000, height=20, text="Azure Interior"))
        run(w, ch.click_text(Page(w), "Azure Interior", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertTrue(item.alive, "the dropdown must stay open")
        self.assertEqual(wheels(w), [])
        self.assertEqual(w.clicked, ["item"])

    def test_regression_dropdown_closing_raises_and_never_clicks_other_match(self):
        w = World(scroll=1000)
        w.add(El("item", top=975 - VIS_TOP + 1000, height=30, text="Azure Interior",
                 closes_on_scroll=True))                             # viewport 975..1005: needs a notch
        w.add(El("other", top=3000, height=20, text="Azure Interior"))
        with self.assertRaises(RuntimeError):
            run(w, ch.click_text(Page(w), "Azure Interior", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, [], "no click: not the other match, not the stale point")

    # 🟠 2 — the 1080 px fallback / emulated viewport put the target on or below the bottom edge
    # of the captured frame; with no_viewport the real height is innerHeight (992).
    def test_regression_viewport_none_uses_inner_height_target_fully_in_frame(self):
        w = World(viewport_size=False)
        far = w.add(El("far", top=3000, height=40))
        run(w, ch.click_box(Page(w), Locator(w, far), ch.CursorState(*w.mouse_pos), **fast()))
        r = w.rect(far)
        self.assertLessEqual(r["y"] + r["height"], H, "target must end fully inside innerHeight")
        self.assertEqual(w.clicked, ["far"])

    def test_regression_scroll_through_centre_from_inner_height(self):
        w = World(viewport_size=False, max_scroll=240)
        cursor = ch.CursorState()
        run(w, ch.scroll_through(Page(w), cursor, pause=0, hold=0))
        self.assertEqual((cursor.x, cursor.y), (W / 2, H / 2))
        self.assertTrue(all(e[1] == [W / 2, H / 2] for e in w.events if e[0] == "scrollpos"))


# ---------------------------------------------------------------------------------------------
# Round-3 review: settle inside the pad, end-of-page acceptance, middle of the visible part,
# iframe coordinates, fixed popper inside a clipping ancestor.
# (1), (4) are Python changes (red on the round-2 module); (2) guards the new settle path;
# (3), (5) live in _TARGET_STATE_JS — here they pin the Python TWIN of that JS only.
# ---------------------------------------------------------------------------------------------
class TestRound3Regression(unittest.TestCase):
    def _click(self, w, el, world=None, **kw):
        run(w, ch.click_box(Page(w), Locator(world or w, el), ch.CursorState(*w.mouse_pos), **fast(**kw)))

    # (1) after scrolling, the target settles inside the pad — not flush with the frame edge
    def test_regression_scroll_down_settles_inside_pad_not_flush_bottom(self):
        w = World()
        el = w.add(El("t", top=3000, height=40))
        self._click(w, el)
        r = w.rect(el)
        self.assertGreaterEqual(r["y"], VIS_TOP + 40)
        self.assertLessEqual(r["y"] + r["height"], H - 40, "944..984 of 992 = flush with the frame bottom")
        self.assertEqual(w.clicked, ["t"])

    def test_regression_scroll_up_settles_inside_pad_not_flush_under_panel(self):
        w = World(scroll=5000)
        el = w.add(El("t", top=200, height=34))
        self._click(w, el)
        self.assertGreaterEqual(w.rect(el)["y"], VIS_TOP + 40, "top == visTop = glued under the panel")
        self.assertEqual(w.clicked, ["t"])

    def test_no_scroll_needed_still_accepts_edge_without_settling(self):
        w = World()
        el = w.add(El("t", top=955 - VIS_TOP, height=30))      # 955..985: visible, inside no pad
        self._click(w, el)
        self.assertEqual(wheels(w), [])
        self.assertEqual(w.clicked, ["t"])

    # (2) page cannot scroll into the pad: the visible target is accepted, no force, no raise
    def test_end_of_page_visible_target_accepted_without_force(self):
        w = World(max_scroll=2000)
        el = w.add(El("t", top=930 - VIS_TOP + 2000, height=40))   # 930..970 at max scroll
        self._click(w, el)
        self.assertEqual(w.scroll, 2000)
        self.assertEqual(w.forced, 0)
        self.assertEqual(w.clicked, ["t"])

    # Settle must never refuse a fully visible, hit-tested element. Pixel-snapped scrolling
    # (DPR 1) + a fractional element position leaves |need| < 1 when the pad can only be met at
    # one exact position (height between region - 2*pad and region, or a small scroller).
    def test_settle_visible_hit_within_one_px_of_exact_slot_is_accepted(self):
        self.assertEqual(ch.scroll_delta(123.5, 973.5, 104, 992, hit=True, settle=True), 0)
        self.assertEqual(ch.scroll_delta(35.4, 65.4, 0, 100, hit=True, settle=True), 0)

    def test_settle_with_pixel_snapped_scroll_and_fractional_position_does_not_raise(self):
        w = World(snap=True)
        el = w.add(El("t", top=3000.5, height=850))
        self._click(w, el)
        self.assertEqual(w.clicked, ["t"])

    # (3) tall element: click the middle of the VISIBLE part, not the last row of the frame
    def test_tall_partly_visible_clicked_at_middle_of_visible_part(self):
        w = World()
        el = w.add(El("tall", top=500 - VIS_TOP, height=1200, x=400, width=600))   # 500..1700
        self._click(w, el)
        down = next(e for e in w.events if e[0] == "down")
        self.assertAlmostEqual(down[2], (500 + H) / 2)          # 746, not 991
        self.assertAlmostEqual(down[1], 700)
        self.assertEqual(w.clicked, ["tall"])

    # (4) iframe: bring the <iframe> into view first, then click in MAIN-page coordinates
    def _iframe_world(self, mouse_pos=(1500.0, 600.0)):
        # cursor starts beside the iframe column (x 300..1100) so the main stage is not captured
        w = World(mouse_pos=mouse_pos)
        frame_el = El("iframe", top=2500, height=600, x=300, width=800)
        child = w.add_frame(frame_el, World(width=800, inner_h=600, vis_top=0, max_scroll=5000))
        btn = child.add(El("btn", top=1500, height=30, x=100, width=120))
        return w, frame_el, child, btn

    def test_iframe_passing_under_cursor_still_clicks_target(self):
        # Characterization: with the cursor in the iframe's path, main-stage wheels land on the
        # iframe (it scrolls its own content), the main stage gets stuck and falls back to one
        # scrollIntoView jump. The click is still correct.
        w, _fe, child, btn = self._iframe_world(mouse_pos=(W / 2, 600.0))
        self._click(w, btn, world=child)
        self.assertEqual(w.clicked, ["frame:btn"])
        self.assertLessEqual(w.forced, 1)

    def test_regression_iframe_target_clicked_in_main_page_coordinates(self):
        w, frame_el, child, btn = self._iframe_world()
        self._click(w, btn, world=child)
        kinds = [e[0] for e in w.events]
        self.assertIn("frame_element", kinds, "the <iframe> element must be brought into view")
        self.assertLess(kinds.index("frame_element"), kinds.index("wheel"), "iframe element first")
        fr = w.rect(frame_el)
        self.assertGreaterEqual(fr["y"], VIS_TOP)
        self.assertLessEqual(fr["y"] + fr["height"], H, "the <iframe> must end inside the frame")
        self.assertGreater(child.scroll, 0, "inner page scrolled by wheels over the iframe")
        self.assertEqual((w.forced, child.forced), (0, 0))
        self.assertEqual(w.clicked, ["frame:btn"])

    def test_iframe_bring_into_view_returns_translated_point_and_wheels_inside_iframe(self):
        w, frame_el, child, btn = self._iframe_world()
        x, y = run(w, ch.bring_into_view(Page(w), Handle(child, btn), ch.CursorState(*w.mouse_pos), pause=0))
        fr, br = w.rect(frame_el), child.rect(btn)
        self.assertAlmostEqual(x, fr["x"] + br["x"] + br["width"] / 2)
        self.assertAlmostEqual(y, fr["y"] + br["y"] + br["height"] / 2)
        inner = [e for e in w.events if e[0] == "wheel"][-1]
        px, py = inner[2]
        self.assertTrue(fr["x"] <= px <= fr["x"] + fr["width"] and fr["y"] <= py <= fr["y"] + fr["height"],
                        "the cursor box is translated: the last wheel is over the iframe")

    def test_iframe_target_without_bounding_box_raises_without_click(self):
        class NoBox(Handle):
            async def bounding_box(self):
                return None

        w, _fe, child, btn = self._iframe_world()
        with self.assertRaises(RuntimeError):
            run(w, ch.bring_into_view(Page(w), NoBox(child, btn), ch.CursorState(*w.mouse_pos), pause=0))
        self.assertEqual(w.clicked, [])

    def test_owner_frame_none_treated_as_main_frame(self):
        w = World()
        w.frame = None
        el = w.add(El("t", top=3000))
        self._click(w, el)
        self.assertEqual(w.clicked, ["t"])

    # (5) Odoo 17 popper: position:fixed item inside a clipping `.table-responsive` (not its
    # containing block) — fully visible, no wheel (a wheel closes the AutoComplete).
    def test_fixed_popper_inside_clipping_ancestor_clicked_without_wheel(self):
        w = World(scroll=1000)
        item = w.add(El("item", top=700, height=30, layer="fixed", text="Azure Interior",
                        closes_on_scroll=True, clip=(400, 600)))
        run(w, ch.click_text(Page(w), "Azure Interior", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(wheels(w), [])
        self.assertTrue(item.alive)
        self.assertEqual(w.clicked, ["item"])

    def test_fixed_element_clipped_by_its_containing_block_is_not_visible(self):
        w = World(scroll=1000)
        w.add(El("item", top=700, height=30, layer="fixed", text="Azure Interior",
                 closes_on_scroll=True, clip=(400, 600), clip_is_cb=True))
        with self.assertRaises(RuntimeError):
            run(w, ch.click_text(Page(w), "Azure Interior", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, [])


# ---------------------------------------------------------------------------------------------
# Round-4 review: a tall auto-height iframe (Email Marketing html field) is "visible" inside
# its own frame while the point is off-screen or under the parent's control panel — the point
# must hit-test through the WHOLE frame chain; one scroll_into_view_if_needed jump, then raise.
# ---------------------------------------------------------------------------------------------
class TestRound4Regression(unittest.TestCase):
    def _tall_iframe(self, scroll=0, iframe_top=200):
        # auto-height iframe: 2000 px tall, its own viewport = 800 x 2000, cannot scroll inside
        w = World(scroll=scroll, mouse_pos=(1500.0, 600.0))
        fe = El("iframe", top=iframe_top, height=2000, x=300, width=800)
        child = w.add_frame(fe, World(width=800, inner_h=2000, vis_top=0, max_scroll=0))
        btn = child.add(El("btn", top=1700, height=30, x=100, width=120))
        return w, fe, child, btn

    def _click(self, w, child, btn):
        run(w, ch.click_box(Page(w), Locator(child, btn), ch.CursorState(*w.mouse_pos), **fast()))

    def test_regression_tall_iframe_target_below_fold_clicked_inside_frame(self):
        w, _fe, child, btn = self._tall_iframe()
        self._click(w, child, btn)
        self.assertEqual(w.clicked, ["frame:btn"], "never a main-page / off-screen click")
        self.assertEqual([e for e in w.events if e[0] == "jump"], [("jump",)], "one jump, not more")

    def test_regression_tall_iframe_target_under_control_panel_never_clicks_panel(self):
        # parent scrolled so the target sits at main-page y 60..90 (under the 104 px panel)
        w, fe, child, btn = self._tall_iframe(scroll=VIS_TOP + 200 + 1640)
        self.assertEqual(w.rect(fe)["y"] + 1700, 60)
        try:
            self._click(w, child, btn)
        except RuntimeError:
            self.assertEqual(w.clicked, [])
            return
        self.assertEqual(w.clicked, ["frame:btn"])
        self.assertNotIn("panel", w.clicked)

    def test_regression_frame_chain_miss_after_jump_raises_without_click(self):
        # after the jump the target is centred on a main-page band covered by a backdrop:
        # the in-frame state says visible + hit, the chain says no → RuntimeError, no click
        w, _fe, child, btn = self._tall_iframe()
        w.covers.append(("backdrop", 500, 600))
        with self.assertRaises(RuntimeError):
            self._click(w, child, btn)
        self.assertEqual(w.clicked, [])
        self.assertEqual(len([e for e in w.events if e[0] == "jump"]), 1, "jump at most once")

    def test_regression_settle_large_fractional_elements_with_px_snapping_clicked(self):
        for height in (850, 850.5, 851, 880):
            for frac in (0.0, 0.3, 0.5):
                with self.subTest(height=height, frac=frac):
                    w = World(snap=True)
                    el = w.add(El("t", top=3000 + frac, height=height))
                    run(w, ch.click_box(Page(w), Locator(w, el), ch.CursorState(*w.mouse_pos), **fast()))
                    self.assertEqual(w.clicked, ["t"])

    def test_iframe_end_of_inner_page_stuck_notch_still_checks_frame_chain(self):
        # Scrollable iframe (viewport 600, max scroll 600) shown at main y 360..960; a main-page
        # sticky footer covers y 900..992. The target ends at the inner page end (local 550..580):
        # the settle notch is stuck inside the frame and chains to the main page. The stuck-notch
        # accept must still hit-test the chain — the first stuck point (main y ≈ 905) is under
        # the footer.
        w = World(mouse_pos=(1500.0, 600.0))
        w.covers.append(("footer", 900, H))
        fe = El("iframe", top=360 - VIS_TOP, height=600, x=300, width=800)
        child = w.add_frame(fe, World(width=800, inner_h=600, vis_top=0, max_scroll=600))
        btn = child.add(El("btn", top=1150, height=30, x=100, width=120))
        run(w, ch.click_box(Page(w), Locator(child, btn), ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, ["frame:btn"])

    def test_iframe_element_without_box_raises_without_click(self):
        class NoBox(Handle):
            async def bounding_box(self):
                return None

        class NoBoxFrame(ChildFrame):
            async def frame_element(self):
                return NoBox(self._pw, self._el)

        w, fe, child, btn = self._tall_iframe()
        btn.top = 300
        child.frame = NoBoxFrame(w, fe)
        with self.assertRaises(RuntimeError):
            self._click(w, child, btn)
        self.assertEqual(w.clicked, [])

    def test_iframe_target_already_visible_in_main_needs_no_jump(self):
        w, _fe, child, btn = self._tall_iframe()
        btn.top = 300                                  # main-page y 304 + 300 = 604: on screen
        self._click(w, child, btn)
        self.assertEqual(w.clicked, ["frame:btn"])
        self.assertNotIn(("jump",), w.events)
        self.assertEqual(wheels(w), [])


# ---------------------------------------------------------------------------------------------
# Round-5 review: an iframe shown with CSS transform: scale (Website "Add page" preview). A
# constant frame→main offset lands off by the scale factor — on a decoy next to the target —
# and the constant-offset chain check still says "hit". Mapping must be proportional.
# ---------------------------------------------------------------------------------------------
class TestRound5Regression(unittest.TestCase):
    def _scaled(self):
        # <iframe> 800 x 600 on the main page at (300, 200) showing a 1600 x 1200 page at scale 0.5
        w = World(mouse_pos=(1500.0, 600.0))
        fe = El("iframe", top=200 - VIS_TOP, height=600, x=300, width=800)
        child = w.add_frame(fe, World(width=1600, inner_h=1200, vis_top=0, max_scroll=0, scale=0.5))
        btn = child.add(El("btn", top=800, height=40, x=1000, width=120))      # main (800,600) 60x20
        child.add(El("decoy", top=800, height=100, x=1120, width=280))        # right of the target
        return w, fe, child, btn

    def test_regression_scaled_iframe_clicks_target_not_decoy(self):
        w, _fe, child, btn = self._scaled()
        run(w, ch.click_box(Page(w), Locator(child, btn), ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, ["frame:btn"], "constant offset lands on the decoy")
        down = next(e for e in w.events if e[0] == "down")
        self.assertAlmostEqual(down[1], 830)                 # 300 + 1060 * 0.5
        self.assertAlmostEqual(down[2], 610)                 # 200 + 820 * 0.5

    def test_regression_hits_chain_scaled_frame_is_proportional(self):
        w, _fe, child, btn = self._scaled()
        h = Handle(child, btn)
        self.assertTrue(run(w, ch._hits_chain(h, 830, 610)), "proportional point is on the target")
        self.assertFalse(run(w, ch._hits_chain(h, 860, 620)), "constant-offset point is on the decoy")
        # one axis at a time: x 860 → frame x 1120 (decoy); y 620 → frame y 840 (below the target)
        self.assertFalse(run(w, ch._hits_chain(h, 860, 610)), "x must be scaled back (×2)")
        self.assertFalse(run(w, ch._hits_chain(h, 830, 620)), "y must be scaled back (×2)")

    def test_scaled_iframe_wheel_box_is_scaled(self):
        # target below the visible part of a scrollable scaled frame: wheels happen over the iframe
        w = World(mouse_pos=(1500.0, 600.0))
        fe = El("iframe", top=200 - VIS_TOP, height=600, x=300, width=800)
        child = w.add_frame(fe, World(width=1600, inner_h=1200, vis_top=0, max_scroll=3000, scale=0.5))
        btn = child.add(El("btn", top=3000, height=40, x=1000, width=120))
        run(w, ch.click_box(Page(w), Locator(child, btn), ch.CursorState(*w.mouse_pos), **fast()))
        for e in w.events:
            if e[0] == "wheel":
                px, py = e[2]
                self.assertTrue(300 <= px <= 1100 and 200 <= py <= 800, f"wheel at {e[2]} outside the iframe")
        self.assertGreater(child.scroll, 0)
        self.assertEqual(w.clicked, ["frame:btn"])

    def test_zero_width_rect_and_zero_box_no_zero_division(self):
        # rWidth 0 (a zero-width element in a frame) → scale falls back to 1.0, never divides by 0
        for name, width, height in (("thin", 0, 40), ("flat", 100, 0)):
            with self.subTest(name=name):
                w, _fe, child, _btn = self._scaled()
                el = child.add(El(name, top=200, height=height, x=200, width=width))
                try:
                    run(w, ch.bring_into_view(Page(w), Handle(child, el), ch.CursorState(*w.mouse_pos), pause=0))
                except RuntimeError:
                    pass                                     # unclickable is fine; ZeroDivisionError is not
                self.assertEqual(w.clicked, [])

        class ZeroBox(Handle):
            async def bounding_box(self):
                return {"x": 400.0, "y": 300.0, "width": 0.0, "height": 0.0}

        w2, _fe2, child2, btn2 = self._scaled()
        self.assertIsInstance(run(w2, ch._hits_chain(ZeroBox(child2, btn2), 400, 300)), bool)


# ---------------------------------------------------------------------------------------------
# Current contract
# ---------------------------------------------------------------------------------------------
class TestScrollDelta(unittest.TestCase):
    def test_fully_visible_and_hit_is_zero_even_at_edges(self):
        self.assertEqual(ch.scroll_delta(104, 140, 104, 992), 0)
        self.assertEqual(ch.scroll_delta(952, 992, 104, 992), 0)
        self.assertEqual(ch.scroll_delta(500, 540, 104, 992, hit=True), 0)

    def test_tall_element_with_hit_is_zero(self):
        self.assertEqual(ch.scroll_delta(-100, 2000, 104, 992, hit=True), 0)

    def test_tall_element_not_hit_moves_toward_middle(self):
        # zone swapped: lo = 992-40-2100 = -1148, hi = 144; top -100 inside → centre 950 vs mid 548
        self.assertEqual(ch.scroll_delta(-100, 2000, 104, 992, hit=False), 120)

    def test_above_scrolls_up_into_padded_zone(self):
        self.assertEqual(ch.scroll_delta(-500, -466, 104, 992, hit=False), -120)
        self.assertEqual(ch.scroll_delta(90, 130, 104, 992, hit=True), -54)       # 90 → 144
        self.assertEqual(ch.scroll_delta(110, 150, 104, 992, hit=False), -34)     # visible, not hit

    def test_below_scrolls_down_into_padded_zone(self):
        self.assertEqual(ch.scroll_delta(2000, 2034, 104, 992, hit=False), 120)
        self.assertEqual(ch.scroll_delta(960, 1000, 104, 992, hit=True), 48)      # 960 → 912

    def test_in_zone_but_covered_moves_centre_to_middle(self):
        self.assertEqual(ch.scroll_delta(500, 540, 104, 992, hit=False), -28)     # 520 → 548
        self.assertEqual(ch.scroll_delta(600, 640, 104, 992, hit=False), 72)

    def test_in_zone_covered_at_middle_returns_none(self):
        self.assertIsNone(ch.scroll_delta(528, 568, 104, 992, hit=False))
        self.assertIsNone(ch.scroll_delta(528.5, 568.5, 104, 992, hit=False))     # |need| 0.5 < 1

    def test_custom_pad_and_step(self):
        self.assertEqual(ch.scroll_delta(0, 50, 100, 600, hit=False, pad=10, step=30), -30)
        self.assertEqual(ch.scroll_delta(95, 145, 100, 600, hit=True, pad=10), -15)

    def test_settle_requires_the_pad_once_scrolled(self):
        self.assertEqual(ch.scroll_delta(950, 990, 104, 992, hit=True), 0)                # not settling
        self.assertEqual(ch.scroll_delta(950, 990, 104, 992, hit=True, settle=True), 38)  # → 912
        self.assertEqual(ch.scroll_delta(110, 150, 104, 992, hit=True, settle=True), -34) # → 144
        self.assertEqual(ch.scroll_delta(500, 540, 104, 992, hit=True, settle=True), 0)
        self.assertEqual(ch.scroll_delta(144, 184, 104, 992, hit=True, settle=True), 0)   # exact pad edges
        self.assertEqual(ch.scroll_delta(912, 952, 104, 992, hit=True, settle=True), 0)

    def test_settle_edge_shrinks_for_large_elements(self):
        # region 888, h 850 → edge = min(40, 19) = 19: 123..973 is accepted
        self.assertEqual(ch.scroll_delta(123, 973, 104, 992, hit=True, settle=True), 0)
        # inside the target zone + hit → accepted, not nudged toward the one exact settle slot
        self.assertEqual(ch.scroll_delta(130, 980, 104, 992, hit=True, settle=True), 0)
        # inside the zone but covered → still moved toward the middle (555 → 548)
        self.assertEqual(ch.scroll_delta(130, 980, 104, 992, hit=False, settle=True), 7)

    def test_settle_does_not_change_tall_or_covered_rules(self):
        self.assertEqual(ch.scroll_delta(-100, 2000, 104, 992, hit=True, settle=True), 0)
        self.assertIsNone(ch.scroll_delta(528, 568, 104, 992, hit=False, settle=True))


class TestViewportDims(unittest.TestCase):
    def test_uses_viewport_size_without_evaluate(self):
        w = World()
        self.assertEqual(run(w, ch.viewport_dims(Page(w))), (W, H))
        self.assertNotIn(("inner_dims",), w.events)

    def test_viewport_none_reads_inner_width_height(self):
        w = World(viewport_size=False, inner_h=900)
        self.assertEqual(run(w, ch.viewport_dims(Page(w))), (W, 900))
        self.assertIn(("inner_dims",), w.events)


class TestBringIntoView(unittest.TestCase):
    def _bring(self, w, el, cursor=None, **kw):
        cursor = cursor or ch.CursorState(*w.mouse_pos)
        return run(w, ch.bring_into_view(Page(w), Handle(w, el), cursor, pause=0, **kw)), cursor

    def test_below_wheels_in_notches_and_returns_hit_tested_point(self):
        w = World()
        el = w.add(El("t", top=3000, height=40))
        (x, y), _ = self._bring(w, el)
        self.assertTrue(all(0 < d <= 120 for d in wheels(w)))
        # Once it has scrolled, it settles INSIDE the 40 px pad: 944..984 would be flush with
        # the frame bottom, one more remainder notch parks it at 912..952.
        self.assertEqual(w.rect(el)["y"], H - 40 - 40)
        self.assertEqual(w.hit(x, y), "t")
        self.assertEqual(w.forced, 0)

    def test_above_wheels_up_and_parks_below_the_panel(self):
        w = World(scroll=5000)
        el = w.add(El("t", top=200, height=34))
        (x, y), _ = self._bring(w, el)
        self.assertTrue(all(-120 <= d < 0 for d in wheels(w)))
        # settles inside the pad, not flush under the control panel (104)
        self.assertEqual(w.rect(el)["y"], VIS_TOP + 40)
        self.assertEqual(w.hit(x, y), "t")

    def test_cursor_outside_scroller_box_glides_into_it_before_first_wheel(self):
        w = World(mouse_pos=(10.0, 20.0))
        el = w.add(El("t", top=3000))
        _, cursor = self._bring(w, el, cursor=ch.CursorState(10, 20))
        first_wheel = next(e for e in w.events if e[0] == "wheel")
        self.assertEqual(first_wheel[2], (W / 2, (VIS_TOP + H) / 2))
        self.assertEqual(w.forced, 0)

    def test_cursor_inside_box_does_not_move_before_wheeling(self):
        w = World(mouse_pos=(300.0, 700.0))
        el = w.add(El("t", top=3000))
        _, cursor = self._bring(w, el, cursor=ch.CursorState(300, 700))
        self.assertEqual(moves(w), [])
        self.assertEqual((cursor.x, cursor.y), (300, 700))

    def test_already_visible_returns_immediately(self):
        w = World()
        el = w.add(El("t", top=300))
        (x, y), _ = self._bring(w, el)
        self.assertEqual(wheels(w), [])
        self.assertEqual(w.hit(x, y), "t")

    def test_one_stuck_notch_forces_scroll_into_view_once_then_succeeds(self):
        w = World(wheel_enabled=False)
        el = w.add(El("t", top=3000))
        (x, y), _ = self._bring(w, el)
        self.assertEqual(len(wheels(w)), 1)
        self.assertEqual(w.forced, 1)
        self.assertEqual(w.hit(x, y), "t")

    def test_second_stuck_notch_raises(self):
        w = World(wheel_enabled=False, force_noop=True)
        el = w.add(El("t", top=3000))
        with self.assertRaises(RuntimeError):
            self._bring(w, el)
        self.assertEqual(len(wheels(w)), 2)
        self.assertEqual(w.forced, 1)

    def test_vanished_element_raises_without_wheel(self):
        w = World()
        el = w.add(El("t", top=3000))
        el.alive = False
        with self.assertRaises(RuntimeError):
            self._bring(w, el)
        self.assertEqual(wheels(w), [])

    def test_covered_at_middle_raises_without_wheel(self):
        w = World()
        w.covers.append(("backdrop", VIS_TOP, H))
        el = w.add(El("t", top=(VIS_TOP + H) / 2 - 20 - VIS_TOP, height=40))
        with self.assertRaises(RuntimeError):
            self._bring(w, el)
        self.assertEqual(wheels(w), [])

    def test_sticky_header_cover_is_scrolled_clear(self):
        w = World(scroll=3000)
        w.covers.append(("sticky", VIS_TOP, VIS_TOP + 46))
        el = w.add(El("t", top=500, height=20))
        (x, y), _ = self._bring(w, el)
        self.assertEqual(w.hit(x, y), "t")

    def test_max_steps_exhausted_raises(self):
        w = World()
        el = w.add(El("t", top=30_000))
        with self.assertRaises(RuntimeError):
            self._bring(w, el, max_steps=3)
        self.assertEqual(wheels(w), [120, 120, 120])

    def test_element_at_document_end_is_reachable(self):
        w = World(max_scroll=2000)
        el = w.add(El("t", top=2000 + (H - VIS_TOP) - 50, height=40))   # 10 px above the doc end
        (x, y), _ = self._bring(w, el)
        self.assertEqual(w.hit(x, y), "t")

    def test_pause_sleeps_between_notches(self):
        w = World()
        el = w.add(El("t", top=1000))
        run(w, ch.bring_into_view(Page(w), Handle(w, el), ch.CursorState(*w.mouse_pos), pause=0.07))
        self.assertEqual(w.sleeps.count(0.07), len(wheels(w)))
        self.assertGreater(len(wheels(w)), 0)

    def test_property_never_returns_a_point_that_does_not_hit(self):
        """Sweep: every outcome is either a hit-tested point or RuntimeError; never a blind point,
        never more wheels than max_steps."""
        cases = 0
        for scroll in (0, 700, 3000):
            for top in (0, 30, 300, 900, 1500, 2950, 6000):
                for height in (20, 34, 300, 1200):
                    for cover in (None, ("sticky", VIS_TOP, VIS_TOP + 46)):
                        w = World(scroll=scroll)
                        if cover:
                            w.covers.append(cover)
                        el = w.add(El("t", top=top, height=height))
                        cases += 1
                        with self.subTest(scroll=scroll, top=top, height=height, cover=bool(cover)):
                            try:
                                (x, y), _ = self._bring(w, el, max_steps=200)
                            except RuntimeError:
                                self.assertIsNotNone(cover, "uncovered reachable target must not fail")
                                continue
                            self.assertEqual(w.hit(x, y), "t")
                            self.assertLessEqual(len(wheels(w)), 200)
        self.assertEqual(cases, 168)


class TestScrollIntoViewAndClickBox(unittest.TestCase):
    def test_scroll_into_view_returns_point(self):
        w = World()
        el = w.add(El("t", top=3000))
        x, y = run(w, ch.scroll_into_view(Page(w), Locator(w, el), ch.CursorState(*w.mouse_pos), pause=0))
        self.assertEqual(w.hit(x, y), "t")

    def test_locator_without_element_raises(self):
        w = World()
        with self.assertRaises(RuntimeError):
            run(w, ch.scroll_into_view(Page(w), Locator(w, None), ch.CursorState()))

    def test_click_box_clicks_the_returned_point(self):
        w = World()
        el = w.add(El("t", top=3000))
        cursor = ch.CursorState(*w.mouse_pos)
        run(w, ch.click_box(Page(w), Locator(w, el), cursor, **fast()))
        self.assertEqual(w.clicked, ["t"])
        last_wheel = max(i for i, e in enumerate(w.events) if e[0] == "wheel")
        down = next(i for i, e in enumerate(w.events) if e[0] == "down")
        self.assertLess(last_wheel, down)

    def test_click_box_tall_element_clicks_hit_tested_point_not_box_centre(self):
        # viewport 500..1700: the box centre (1100) is below the 992 px frame; the hit-tested
        # point is clamped inside the visible area.
        w = World()
        el = w.add(El("tall", top=500 - VIS_TOP, height=1200))
        run(w, ch.click_box(Page(w), Locator(w, el), ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(wheels(w), [])
        self.assertEqual(w.clicked, ["tall"])

    def test_click_box_scroll_false_clicks_raw_box_centre_without_wheel(self):
        w = World()
        el = w.add(El("t", top=3000, height=40, x=500, width=200))
        run(w, ch.click_box(Page(w), Locator(w, el), ch.CursorState(), scroll=False, **fast()))
        self.assertEqual(wheels(w), [])
        down = next(e for e in w.events if e[0] == "down")
        self.assertAlmostEqual(down[1], 600)
        self.assertAlmostEqual(down[2], VIS_TOP + 3000 + 20)

    def test_click_box_scroll_false_hidden_raises(self):
        w = World()
        el = w.add(El("t", top=100))
        el.alive = False
        with self.assertRaises(RuntimeError):
            run(w, ch.click_box(Page(w), Locator(w, el), ch.CursorState(), scroll=False))
        self.assertEqual(w.clicked, [])


class TestTypeInto(unittest.TestCase):
    def _type(self, text, **kw):
        w = World()
        el = w.add(El("f", top=300))
        run(w, ch.type_into(Page(w), Locator(w, el), text, ch.CursorState(*w.mouse_pos), **fast(**kw)))
        return w, [e for e in w.events if e[0] in ("down", "press", "type")]

    def test_clear_default_selects_all_then_types_with_ms_delay(self):
        w, seq = self._type("New name")
        self.assertEqual([e[0] for e in seq], ["down", "press", "type"])
        self.assertEqual(seq[1], ("press", "ControlOrMeta+a"))
        self.assertEqual(seq[2], ("type", "New name", 60.0))
        self.assertEqual(w.clicked, ["f"])

    def test_clear_with_empty_text_also_deletes(self):
        _w, seq = self._type("")
        self.assertEqual(seq[1:], [("press", "ControlOrMeta+a"), ("press", "Delete"), ("type", "", 60.0)])

    def test_clear_false_types_without_pressing(self):
        _w, seq = self._type("abc", clear=False, delay=0.1)
        self.assertEqual([e[0] for e in seq], ["down", "type"])
        self.assertEqual(seq[1], ("type", "abc", 100.0))

    def test_scroll_flag_passthrough(self):
        w = World()
        el = w.add(El("f", top=3000))
        run(w, ch.type_into(Page(w), Locator(w, el), "x", ch.CursorState(), scroll=False, **fast()))
        self.assertEqual(wheels(w), [])


class NestedPage(Page):
    """Inner scroller (max 240) inside the outer one (max 600); the wheel chains outward."""

    def __init__(self, w):
        super().__init__(w)
        self.inner, self.outer = 0, 0
        self.mouse = self

    async def move(self, x, y):
        self.w.mouse_pos = (x, y)

    async def wheel(self, dx, dy):
        self.w.events.append(("wheel", dy, self.w.mouse_pos))
        if dy > 0:
            if self.inner < 240:
                self.inner = min(240, self.inner + dy)
            else:
                self.outer = min(600, self.outer + dy)
        else:
            if self.inner > 0:
                self.inner = max(0, self.inner + dy)
            else:
                self.outer = max(0, self.outer + dy)

    def scroll_positions(self, js):
        return [self.inner, self.outer, 0] if "pos.push" in js else self.inner


class TestScrollThrough(unittest.TestCase):
    def test_wheels_down_until_position_stops_changing(self):
        w = World(max_scroll=500)
        cursor = ch.CursorState()
        run(w, ch.scroll_through(Page(w), cursor, pause=0, hold=1.0))
        self.assertEqual(wheels(w), [120] * 6)
        self.assertEqual(w.scroll, 500)
        self.assertEqual((cursor.x, cursor.y), (W / 2, H / 2))
        self.assertEqual(w.sleeps.count(1.0), 1)

    def test_back_to_top_adds_upward_pass(self):
        w = World(max_scroll=500)
        run(w, ch.scroll_through(Page(w), ch.CursorState(), back_to_top=True, pause=0, hold=1.0))
        self.assertEqual(wheels(w), [120] * 6 + [-120] * 6)
        self.assertEqual(w.scroll, 0)
        self.assertEqual(w.sleeps.count(1.0), 2)

    def test_nested_scroller_keeps_going_until_outer_stops(self):
        w = World()
        page = NestedPage(w)
        run(w, ch.scroll_through(page, ch.CursorState(), back_to_top=True, pause=0, hold=0))
        # down: inner 120,240 → outer 120..600 (5) → one unchanged notch; up mirrors it
        self.assertEqual(wheels(w), [120] * 8 + [-120] * 8)
        self.assertEqual((page.inner, page.outer), (0, 0))

    def test_max_steps_bounds_an_endless_page(self):
        w = World()
        run(w, ch.scroll_through(Page(w), ch.CursorState(), pause=0, hold=0, max_steps=4))
        self.assertEqual(wheels(w), [120] * 4)


class TestWaitReady(unittest.TestCase):
    def test_domcontentloaded_then_visible_selector_then_settle(self):
        w = World()
        run(w, ch.wait_ready(Page(w), ".o_form_view"))
        self.assertEqual(w.events, [("load_state", "domcontentloaded"),
                                    ("selector", ".o_form_view", "visible", 15000.0)])
        self.assertEqual(w.sleeps, [0.5])

    def test_custom_timeout_and_settle(self):
        w = World()
        run(w, ch.wait_ready(Page(w), ".modal", timeout=2.5, settle=0.2))
        self.assertEqual(w.events[-1], ("selector", ".modal", "visible", 2500.0))
        self.assertEqual(w.sleeps, [0.2])

    def test_settle_zero_skips_sleep(self):
        w = World()
        run(w, ch.wait_ready(Page(w), ".o_list_view", settle=0))
        self.assertEqual(w.sleeps, [])


class TestClickText(unittest.TestCase):
    def test_finds_once_with_text_and_case_flag_then_scrolls_that_node(self):
        w = World()
        w.add(El("save", top=3000, height=20, text="Lưu"))
        run(w, ch.click_text(Page(w), "lưu", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.find_args, [["lưu", False]], "one search, no re-search after scrolling")
        self.assertTrue(wheels(w))
        self.assertEqual(w.clicked, ["save"])

    def test_case_sensitive_forwarded(self):
        w = World()
        w.add(El("save", top=300, height=20, text="Lưu"))
        run(w, ch.click_text(Page(w), "Lưu", ch.CursorState(*w.mouse_pos), case_sensitive=True, **fast()))
        self.assertEqual(w.find_args[0], ["Lưu", True])

    def test_scroll_false_clicks_bounding_box_centre_without_wheel(self):
        w = World()
        w.add(El("save", top=3000, height=20, x=600, width=200, text="Lưu"))
        run(w, ch.click_text(Page(w), "Lưu", ch.CursorState(), scroll=False, **fast()))
        self.assertEqual(wheels(w), [])
        down = next(e for e in w.events if e[0] == "down")
        self.assertAlmostEqual(down[1], 700)
        self.assertAlmostEqual(down[2], VIS_TOP + 3000 + 10)

    def test_scroll_false_node_without_box_raises(self):
        w = World()
        w.kill_on_find = True
        w.add(El("save", top=300, height=20, text="Lưu"))
        with self.assertRaises(RuntimeError):
            run(w, ch.click_text(Page(w), "Lưu", ch.CursorState(), scroll=False))
        self.assertEqual(w.clicked, [])

    def test_node_gone_before_scrolling_raises_without_click(self):
        w = World()
        w.kill_on_find = True
        w.add(El("save", top=300, height=20, text="Lưu"))
        with self.assertRaises(RuntimeError):
            run(w, ch.click_text(Page(w), "Lưu", ch.CursorState(), **fast()))
        self.assertEqual(w.clicked, [])

    def test_unique_dropdown_item_closing_raises(self):
        w = World(scroll=1000)
        w.add(El("item", top=975 - VIS_TOP + 1000, height=30, text="Azure", closes_on_scroll=True))
        with self.assertRaises(RuntimeError):
            run(w, ch.click_text(Page(w), "Azure", ch.CursorState(*w.mouse_pos), **fast()))
        self.assertEqual(w.clicked, [])

    def test_not_found_disposes_handle_and_times_out(self):
        w = World()
        with self.assertRaises(TimeoutError):
            run(w, ch.click_text(Page(w), "Không có", ch.CursorState(), timeout=0))
        self.assertGreaterEqual(w.disposed, 1)
        self.assertEqual(w.disposed, len(w.find_args))
        self.assertEqual(w.clicked, [])


if __name__ == "__main__":
    unittest.main()
