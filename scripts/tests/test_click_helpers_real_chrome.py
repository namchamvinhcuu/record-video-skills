#!/usr/bin/env python3
"""Real-Chromium tests for click_helpers.py — the ONLY tests that execute the in-page JS
(_TARGET_STATE_JS, _HIT_AT_JS, the rect JS, _SCROLL_POS_JS, _FIND_LEAF_TEXT_JS). The twin-based
test_click_helpers_scroll.py cannot see a JS bug; every review round of this skill found its
bugs only in real Chrome, so the page shapes below are copied from real-Chrome
probes (Odoo 17 layout: 46 px fixed navbar + 58 px control panel, `.o_content` from 104 px).

Headless Chromium, viewport 1920x992 (the innerHeight of the old non-kiosk recording window; the
default kiosk window is ~the full screen height — the helpers read innerHeight either way, so the
shorter viewport only makes these pages scroll a little sooner). The x11grab geometry of
DemoRecorder (Xvfb + ffmpeg + kiosk) is covered by test_recorder_x11grab_real.py.
Assertions use capture-phase click logs (e.target.id), a wheel counter and scrollTop.

Availability: no `playwright` package or no launchable Chromium (bundled, /usr/bin/google-chrome,
macOS Chrome) → prints one `SKIP-FILE: <reason>` line and exits 0 (run-tests.sh protocol).
Chrome is launched ONCE per file; each test gets a fresh page.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_click_helpers_real_chrome.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "click_helpers.py"
spec = importlib.util.spec_from_file_location("click_helpers_real_chrome_mod", SCRIPT)
assert spec and spec.loader
ch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ch)

VW, VH = 1920, 992
CHROME_FALLBACKS = ("/usr/bin/google-chrome",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LOOP: asyncio.AbstractEventLoop | None = None
PW = None
BROWSER = None


def js_str(s: str) -> str:
    return json.dumps(s).replace("</", "<\\/")      # "</script>" inside a JS literal would end the tag


# ---------------------------------------------------------------- page pieces (from the probes)
HEAD = ("<html><head><style>body{margin:0;font:16px sans-serif} input{height:34px;font-size:16px;box-sizing:border-box}"
        " .menu a{display:block;height:30px;line-height:30px;padding:0 8px}</style></head><body>")
NAV = ("<nav id=nav style='position:fixed;top:0;left:0;right:0;height:46px;background:#714B67;z-index:5'>"
       "<button id=menu_btn style='position:absolute;left:20px;top:8px;height:30px'>MENU</button></nav>"
       "<div id=cp style='position:fixed;top:46px;left:0;right:0;height:58px;background:#eee;z-index:4'>"
       "<button id=new_btn style='position:absolute;left:20px;top:8px;height:31px'>NEW</button>"
       "<input id=cp_search placeholder='Search...' style='position:absolute;left:600px;top:8px;width:700px'></div>")
LOGGER = """<script>
window.__wheels=0; window.__clicks=[]; window.__closed=0;
addEventListener('wheel',()=>__wheels++,{capture:true});
addEventListener('click',e=>__clicks.push(e.target.id||(e.target.tagName+':'+(e.target.textContent||'').trim().slice(0,24))),{capture:true});
</script>"""
OC = "<div class=o_content id=oc style='position:absolute;top:104px;bottom:0;left:0;right:0;overflow:auto'>"
INNER_LOG = "<script>window.__in=[]; document.addEventListener('click',e=>{ window.__in.push(e.target.id||e.target.tagName); });</script>"

PAGE_LONG = (HEAD + NAV + OC + "<div style='height:3000px;background:linear-gradient(#fff,#ccc)'></div>"
             "<button id=far_btn style='height:40px;width:300px'>FAR</button>"
             "<div style='height:600px'></div></div>" + LOGGER + "</body></html>")   # room to reach the pad

PAGE_UP = (HEAD + NAV + OC + "<div style='height:1500px;background:linear-gradient(#fff,#eee)'></div>"
           "<div style='padding-left:700px'><input id=partner value='Old name' style='width:400px'></div>"
           "<div style='height:4200px'></div></div>" + LOGGER + "</body></html>")

ROWS = "".join(f"<tr><td id=r{i} style='height:32px'>Row {i}</td></tr>" for i in range(120))
PAGE_STICKY = (HEAD + NAV + OC + "<table style='width:100%;border-collapse:collapse'>"
               "<thead><tr><th id=hdr style='position:sticky;top:0;height:60px;background:#ddd;z-index:1'>Header</th></tr></thead>"
               f"<tbody>{ROWS}</tbody></table></div>" + LOGGER + "</body></html>")

# Odoo 17 AutoComplete: the menu is a `position: fixed` <ul> rendered INSIDE the field (inside the
# one2many `.table-responsive`, overflow-x:auto ⇒ overflow-y auto); any scroll outside the root closes it.
ITEMS = [f"Product {i}" for i in range(1, 9)] + ["Search More..."]
AC_JS = """<script>
function acSetup(rootId, inputId) {
  const root = document.getElementById(rootId), inp = document.getElementById(inputId);
  inp.addEventListener('click', () => {
    if (root.querySelector('.menu')) return;
    const r = inp.getBoundingClientRect();
    const ul = document.createElement('ul');
    ul.className = 'menu o-autocomplete--dropdown-menu';
    ul.style.cssText = `position:fixed;margin:0;padding:0;list-style:none;left:${r.left}px;top:${r.bottom}px;width:380px;background:#fff;border:1px solid #888;z-index:10`;
    ul.innerHTML = %ITEMS%.map(t => `<li><a>${t}</a></li>`).join('');
    root.appendChild(ul);
  });
  addEventListener('scroll', e => {
    const ul = root.querySelector('.menu');
    if (ul && !root.contains(e.target)) { ul.remove(); window.__closed++; }
  }, true);
}
</script>""".replace("%ITEMS%", json.dumps(ITEMS))
PAGE_LIST_M2O = (HEAD + NAV + OC +
                 "<div style='height:320px;padding:20px 300px'>Customer, dates, pricelist</div>"
                 "<div class=o_field_x2many_list style='margin:0 300px'>"
                 "<div class='o_list_renderer table-responsive' id=lr style='overflow-x:auto'>"
                 "<table style='width:100%;border-collapse:collapse'>"
                 "<thead><tr><th style='text-align:left;height:36px'>Product</th><th>Quantity</th></tr></thead>"
                 "<tbody><tr class='o_data_row o_selected_row'><td class=o_data_cell style='width:400px'>"
                 "<div class=o-autocomplete id=ac_root><input id=prod style='width:380px'></div></td><td>1.00</td></tr>"
                 "<tr><td colspan=2 style='height:36px'><a id=add_line>Add a line</a></td></tr></tbody></table></div></div>"
                 "<div style='height:1500px'></div></div>"
                 + LOGGER + AC_JS + "<script>acSetup('ac_root','prod')</script></body></html>")

PAGE_TALL = (HEAD + NAV + OC + "<div style='height:200px'></div>"
             "<textarea id=tall style='display:block;margin-left:300px;width:600px;height:1500px'>line 1</textarea>"
             "<div style='height:1500px'></div></div>" + LOGGER + "</body></html>")

M2O_ITEMS = ["Azure Interior", "Brandon Freeman", "Gemini Furniture", "Wood Corner"]


def page_m2o(menu_top_px: int) -> str:
    """Absolute dropdown under the input that closes on any outer scroll; 'Wood Corner' also
    appears further down the page (a wrong re-search would click it)."""
    return (HEAD + NAV + OC + f"<div style='height:{menu_top_px}px'></div>"
            "<div id=ac_root style='position:relative;margin-left:300px'>"
            "<input id=m2o style='width:400px'>"
            "<div id=ac_menu style='position:absolute;top:34px;left:0;width:400px;background:#fff;border:1px solid #888;display:none;z-index:3'>"
            + "".join(f"<a style='display:block;height:30px;line-height:30px'>{n}</a>" for n in M2O_ITEMS) +
            "</div></div><div style='height:1500px'></div>"
            "<p id=elsewhere><span>Wood Corner</span> mentioned elsewhere</p><div style='height:600px'></div></div>"
            + LOGGER +
            "<script>m2o.addEventListener('click',()=>{ac_menu.style.display='block'});"
            "window.addEventListener('scroll',e=>{ if(ac_menu.style.display==='block' && !ac_root.contains(e.target))"
            "{ac_menu.style.display='none';} },true);</script></body></html>")


def btn_doc(top_px: int, total_px: int) -> str:
    return (f"<html><body style='margin:0;background:#ffe'><div style='height:{total_px}px;position:relative'>"
            f"<button id=b style='position:absolute;left:40px;top:{top_px}px;width:200px;height:40px'>INSIDE</button>"
            "</div>" + INNER_LOG + "</body></html>")


def page_iframe(iframe_style: str, doc: str, before_px: int = 200) -> str:
    return (HEAD + NAV + OC + f"<div style='height:{before_px}px'></div>"
            f"<iframe id=f style='display:block;margin-left:400px;width:600px;border:0;{iframe_style}'></iframe>"
            "<div style='height:2000px'></div></div>" + LOGGER +
            "<script>document.getElementById('f').srcdoc = " + js_str(doc) + ";</script></body></html>")


DOC_SCALED = ("<html><body style='margin:0;background:#ffe;width:800px;height:800px;position:relative'>"
              "<button id=b style='position:absolute;left:400px;top:400px;width:200px;height:40px'>TARGET</button>"
              "<button id=decoy style='position:absolute;left:580px;top:425px;width:200px;height:60px'>DECOY</button>"
              + INNER_LOG + "</body></html>")
NESTED_OUTER = ("<html><body style='margin:0;background:#eef'><div style='height:150px'>outer frame</div>"
                "<iframe id=i style='display:block;margin-left:100px;width:400px;height:250px;border:2px solid #900'></iframe>"
                "<script>document.getElementById('i').srcdoc = " + js_str(btn_doc(60, 300)) + ";</script></body></html>")

PAGE_NESTED = (HEAD + NAV + OC + "<div style='height:900px'></div>"
               "<div id=inner style='height:400px;overflow:auto;margin:0 200px;border:3px solid red'>"
               "<div style='height:1200px'></div></div><div style='height:3000px'></div></div>"
               + LOGGER + "</body></html>")

FAST = {"steps": 2, "duration": 0}


class RealChromeCase(unittest.TestCase):
    def setUp(self):
        self.page = self.run_(BROWSER.new_page(viewport={"width": VW, "height": VH}))

    def tearDown(self):
        self.run_(self.page.close())

    @staticmethod
    def run_(coro):
        return LOOP.run_until_complete(coro)

    def load(self, html: str, cursor=(960.0, 600.0)):
        async def go():
            await self.page.set_content(html, wait_until="load")
            await self.page.mouse.move(*cursor)
        self.run_(go())
        return ch.CursorState(*cursor)

    def ev(self, js: str, arg=None):
        return self.run_(self.page.evaluate(js, arg))

    def reset_logs(self):
        self.ev("()=>{__wheels=0; __clicks.length=0; __closed=0;}")

    def logs(self):
        return self.ev("()=>({wheels: __wheels, clicks: __clicks, closed: __closed,"
                       " oc: document.getElementById('oc').scrollTop})")

    def rect(self, sel: str):
        return self.ev("(s)=>{const r=document.querySelector(s).getBoundingClientRect(); return [r.top, r.bottom];}", sel)


class TestOdooLayout(RealChromeCase):
    def test_scroll_down_far_button_clicked_by_wheel_inside_pad(self):
        cur = self.load(PAGE_LONG, cursor=(30.0, 20.0))          # parked over the navbar
        self.run_(ch.click_box(self.page, self.page.locator("#far_btn"), cur, **FAST))
        lg = self.logs()
        top, bottom = self.rect("#far_btn")
        self.assertEqual(lg["clicks"], ["far_btn"])
        # 3104 → ≤ 912 is ~2190 px ≈ 19 notches of 120; a jump fallback would take 1-2 wheels
        self.assertGreaterEqual(lg["wheels"], 15, "scrolled by the wheel (smooth), not by a jump")
        self.assertGreaterEqual(top, 104 + 40)
        self.assertLessEqual(bottom, VH - 40)

    def test_scroll_up_to_input_under_panel_type_into_replaces_text(self):
        cur = self.load(PAGE_UP)
        self.ev("()=>{document.getElementById('oc').scrollTop=3000}")
        self.reset_logs()
        self.run_(ch.type_into(self.page, self.page.locator("#partner"), "Azure", cur, delay=0.005, **FAST))
        lg = self.logs()
        self.assertEqual(lg["clicks"], ["partner"], "the input, not the control panel")
        self.assertEqual(self.run_(self.page.locator("#partner").input_value()), "Azure")
        self.assertEqual(self.run_(self.page.locator("#cp_search").input_value()), "")
        self.assertGreater(lg["wheels"], 0)

    def test_navbar_and_control_panel_buttons_no_wheel_no_jitter(self):
        cur = self.load(PAGE_UP)
        self.ev("()=>{document.getElementById('oc').scrollTop=600}")
        self.reset_logs()
        for sel in ("#menu_btn", "#new_btn", "#cp_search"):
            self.run_(ch.click_box(self.page, self.page.locator(sel), cur, **FAST))
        lg = self.logs()
        self.assertEqual(lg["wheels"], 0)
        self.assertEqual(lg["oc"], 600)
        self.assertEqual(lg["clicks"], ["menu_btn", "new_btn", "cp_search"])

    def test_scroll_up_under_sticky_header_clicks_the_row(self):
        cur = self.load(PAGE_STICKY)
        self.ev("()=>{document.getElementById('oc').scrollTop=3000}")
        self.reset_logs()
        self.run_(ch.click_box(self.page, self.page.locator("#r40"), cur, **FAST))
        lg = self.logs()
        self.assertEqual(lg["clicks"], ["r40"], "the row, not the 60 px sticky header")
        self.assertGreaterEqual(self.rect("#r40")[0], 104 + 60)


class TestFixedPopperAndTallField(RealChromeCase):
    def test_fixed_popper_inside_table_responsive_clicked_without_wheel(self):
        cur = self.load(PAGE_LIST_M2O)
        self.run_(ch.click_box(self.page, self.page.locator("#prod"), cur, **FAST))   # opens the menu
        lr_bottom = self.rect("#lr")[1]
        item_top = self.ev("()=>[...document.querySelectorAll('.menu a')].find(a=>a.textContent==='Product 6')"
                           ".getBoundingClientRect().top")
        self.assertGreater(item_top, lr_bottom, "premise: the item lies outside the clipping .table-responsive")
        self.reset_logs()
        self.run_(ch.click_text(self.page, "Product 6", cur, **FAST))
        lg = self.logs()
        self.assertEqual(lg["wheels"], 0, "a wheel closes the Odoo AutoComplete")
        self.assertEqual(lg["closed"], 0)
        self.assertEqual(lg["clicks"], ["A:Product 6"])

    def test_tall_field_clicked_in_middle_of_visible_part(self):
        cur = self.load(PAGE_TALL, cursor=(960.0, 500.0))
        top, bottom = self.rect("#tall")
        expected_y = (max(top, 104) + min(bottom, VH)) / 2         # 304..992 → 648
        self.reset_logs()
        self.run_(ch.click_box(self.page, self.page.locator("#tall"), cur, **FAST))
        lg = self.logs()
        self.assertEqual(lg["wheels"], 0)
        self.assertEqual(lg["clicks"], ["tall"])
        self.assertAlmostEqual(cur.y, expected_y, delta=2, msg="not the clamped centre at the frame edge (991)")


class TestDropdown(RealChromeCase):
    def test_visible_dropdown_item_clicked_without_wheel(self):
        cur = self.load(page_m2o(300))
        self.run_(ch.click_box(self.page, self.page.locator("#m2o"), cur, **FAST))
        self.reset_logs()
        self.run_(ch.click_text(self.page, "Wood Corner", cur, **FAST))
        lg = self.logs()
        self.assertEqual(lg["wheels"], 0)
        self.assertEqual(lg["clicks"], ["A:Wood Corner"])

    def test_offscreen_item_closing_on_scroll_raises_and_never_clicks(self):
        cur = self.load(page_m2o(VH - 104 - 80))
        self.run_(ch.click_box(self.page, self.page.locator("#m2o"), cur, **FAST))
        self.reset_logs()
        with self.assertRaises(RuntimeError):
            self.run_(ch.click_text(self.page, "Wood Corner", cur, **FAST))
        self.assertEqual(self.logs()["clicks"], [], "not the duplicate text, not the stale point")


class TestIframes(RealChromeCase):
    def _frame_click(self, html, frames=("#f",), prep=None):
        cur = self.load(html)
        fl = self.page
        for sel in frames:
            fl = fl.frame_locator(sel)
        btn, body = fl.locator("#b"), fl.locator("body")
        self.run_(btn.wait_for(timeout=5000))
        if prep:
            self.ev(prep)
        self.reset_logs()
        self.bbox_before = self.run_(btn.bounding_box())
        err = None
        try:
            self.run_(ch.click_box(self.page, btn, cur, **FAST))
        except RuntimeError as e:
            err = e
        self.click_point = (cur.x, cur.y)
        return err, self.run_(body.evaluate("()=>window.__in")), self.logs()

    def test_fixed_size_iframe_scrolling_inside(self):
        err, inside, lg = self._frame_click(page_iframe("height:300px", btn_doc(800, 1400)))
        self.assertIsNone(err)
        self.assertEqual(inside, ["b"])
        self.assertEqual(lg["clicks"], [])

    def test_tall_auto_height_iframe_target_below_fold(self):
        err, inside, lg = self._frame_click(page_iframe("height:1600px", btn_doc(1300, 1600)))
        self.assertIsNone(err)
        self.assertEqual(inside, ["b"], "clicked inside the frame, never a main-page point")
        self.assertEqual(lg["clicks"], [])

    def test_tall_iframe_target_under_control_panel_never_clicks_panel(self):
        err, inside, lg = self._frame_click(page_iframe("height:1600px", btn_doc(40, 1600)),
                                            prep="()=>{document.getElementById('oc').scrollTop = 282}")
        self.assertNotIn("cp", lg["clicks"])
        self.assertEqual(lg["clicks"], [])
        if err is None:
            self.assertEqual(inside, ["b"])
        else:
            self.assertEqual(inside, [])

    def test_scaled_iframe_clicks_target_not_decoy(self):
        err, inside, lg = self._frame_click(page_iframe(
            "width:800px;height:800px;transform:scale(0.5);transform-origin:0 0", DOC_SCALED))
        self.assertIsNone(err)
        self.assertEqual(inside, ["b"])
        # fully visible, no scroll: the click point is the centre of the target's MAIN-page box
        bb = self.bbox_before
        self.assertAlmostEqual(self.click_point[0], bb["x"] + bb["width"] / 2, delta=2)
        self.assertAlmostEqual(self.click_point[1], bb["y"] + bb["height"] / 2, delta=2)

    def test_nested_iframes(self):
        err, inside, lg = self._frame_click(page_iframe("height:450px", NESTED_OUTER), frames=("#f", "#i"))
        self.assertIsNone(err)
        self.assertEqual(inside, ["b"])
        self.assertEqual(lg["clicks"], [])


MOVE_LOG = "<script>window.__mv=null; addEventListener('mousemove',e=>{__mv=[e.clientX,e.clientY]},{capture:true});</script>"


def page_value(left_px: int, top_px: int = 300, width_px: int = 120) -> str:
    """An Odoo-like numeric cell holding the value the narration talks about."""
    return (HEAD + NAV + OC + f"<div style='height:{top_px}px'></div>"
            f"<span id=val style='display:inline-block;margin-left:{left_px}px;width:{width_px}px;height:34px;"
            "line-height:34px;text-align:right;background:#def'>677.5</span>"
            "<div style='height:1500px'></div></div>" + LOGGER + MOVE_LOG + "</body></html>")


class TestPointAt(RealChromeCase):
    """point_at parks the cursor BESIDE the value (never on it) and never clicks — the cursor
    covered "677.5" in a real take."""

    def _point(self, html, sel="#val", cursor=(960.0, 600.0), **kw):
        cur = self.load(html, cursor=cursor)
        self.reset_logs()
        self.run_(ch.point_at(self.page, self.page.locator(sel), cur, hold=0, **FAST, **kw))
        box = self.run_(self.page.locator(sel).bounding_box())
        hit = self.ev("([x,y,s])=>{const e=document.elementFromPoint(x,y), t=document.querySelector(s);"
                      " return !!e && (e===t || t.contains(e));}", [cur.x, cur.y, sel])
        return cur, box, hit, self.logs(), self.ev("()=>__mv")

    def test_parks_right_of_value_not_on_it_and_never_clicks(self):
        cur, box, hit, lg, mv = self._point(page_value(600))
        right = box["x"] + box["width"]
        self.assertFalse(hit, "elementFromPoint at the cursor is the value — the dot covers it")
        self.assertGreater(cur.x, right)
        self.assertLessEqual(cur.x - right, ch.POINT_GAP + 1)
        self.assertAlmostEqual(cur.y, box["y"] + box["height"] / 2, delta=1)
        self.assertEqual(lg["clicks"], [], "point_at must not click")
        self.assertEqual([round(v) for v in mv], [round(cur.x), round(cur.y)], "the real mouse ended there")

    def test_value_at_right_screen_edge_parks_left(self):
        cur, box, hit, lg, _ = self._point(page_value(VW - 130, width_px=125))
        self.assertFalse(hit)
        self.assertLess(cur.x, box["x"])
        self.assertLessEqual(box["x"] - cur.x, ch.POINT_GAP + 1)
        self.assertEqual(lg["clicks"], [])

    def test_value_below_the_fold_scrolled_into_view_then_pointed(self):
        cur, box, hit, lg, _ = self._point(page_value(600, top_px=2400))
        self.assertGreaterEqual(box["y"], 104, "scrolled into the visible page area first")
        self.assertLessEqual(box["y"] + box["height"], VH)
        self.assertFalse(hit)
        self.assertLessEqual(cur.x - (box["x"] + box["width"]), ch.POINT_GAP + 1)
        self.assertGreater(lg["wheels"], 0)
        self.assertEqual(lg["clicks"], [])


# Odoo 17 list rows (and wide dialogs) are wider than half the page. The icon font
# draws through ::before (generated content — NO DOM text node), like `.o_row_handle.fa-sort`.
WIDE_CSS = ("<style>table.o_list_table{width:100%;border-collapse:collapse;table-layout:fixed}"
            ".o_list_table td{height:32px;border-bottom:1px solid #ddd;padding:0 8px;white-space:nowrap}"
            ".fa-sort::before{content:'\\2195'}</style>")
HANDLE_TD = "<td class=o_handle_cell style='width:40px'><span class='o_row_handle fa fa-sort'></span></td>\n    "
SELECTOR_TD = "<td class=o_list_record_selector style='width:40px'><input type=checkbox></td>\n    "


def page_wide_row(cells: str, top_px: int = 300) -> str:
    return (HEAD + WIDE_CSS + NAV + OC + f"<div style='height:{top_px}px'></div>"
            "<table class=o_list_table><tbody>\n  <tr class=o_data_row id=row>\n    " + cells + "\n  </tr>\n</tbody></table>"
            "<div style='height:1500px'></div></div>" + LOGGER + MOVE_LOG + "</body></html>")


class TestPointAtWideElement(RealChromeCase):
    """Regression: point_at on a full-width `.o_data_row` (any element wider than
    half the page; Odoo 17 kanban columns are ~320 px, so not those) parked
    the cursor at x = gap/2 (the row's left edge, no room on its right) — pointing at nothing. It now
    points beside the first VISIBLE text inside the element; no text → the old element box."""

    def _point(self, html, sel="#row", text_sel="#t", frame=None, **kw):
        cur = self.load(html)
        root = self.page.frame_locator(frame) if frame else self.page
        loc = root.locator(sel)
        self.run_(loc.wait_for(timeout=5000))
        self.reset_logs()
        self.run_(ch.point_at(self.page, loc, cur, hold=0, **FAST, **kw))
        tbox = self.run_(root.locator(text_sel).bounding_box()) if text_sel else None
        return cur, tbox, self.run_(loc.bounding_box()), self.logs(), self.ev("()=>window.__mv")

    def assert_beside_text(self, cur, tbox):
        right = tbox["x"] + tbox["width"]
        self.assertGreater(cur.x, right, f"cursor {cur.x:.1f} not right of the text (ends {right:.1f})")
        self.assertLessEqual(cur.x - right, ch.POINT_GAP + 1)
        self.assertAlmostEqual(cur.y, tbox["y"] + tbox["height"] / 2, delta=1.5)

    def test_full_width_row_points_beside_first_text_after_icon_handle_cell_fix(self):
        cells = (HANDLE_TD + SELECTOR_TD + "<td style='width:600px'></td>\n    "
                 "<td class=o_data_cell><span id=t>QC/2026/0001</span></td>\n    <td class=o_data_cell>Inspection lot</td>")
        cur, tbox, rbox, lg, mv = self._point(page_wide_row(cells))
        self.assertGreater(rbox["width"], VW / 2, "premise: the row is wider than half the page")
        self.assertGreater(tbox["x"], 600, "premise: the first text sits mid-row")
        self.assert_beside_text(cur, tbox)
        self.assertGreater(cur.x, 600, "never back at the left edge (x = gap/2)")
        hit_text = self.ev("([x,y])=>{const e=document.elementFromPoint(x,y); return !!e && e.id==='t';}", [cur.x, cur.y])
        self.assertFalse(hit_text, "the dot sits beside the text, not on it")
        self.assertEqual(lg["clicks"], [], "point_at must not click")
        self.assertLessEqual(max(abs(mv[0] - cur.x), abs(mv[1] - cur.y)), 1, f"the real mouse ended there: {mv}")

    def test_hidden_blank_and_zero_size_text_is_skipped(self):
        cells = (HANDLE_TD + "<td style='width:80px'>&nbsp;</td>\n    "              # blank (trim() drops NBSP)
                 "<td style='width:120px'><span style='visibility:hidden'>HIDDEN</span>   </td>\n    "
                 "<td style='display:none'>GONE</td>\n    <td style='width:120px'><div style='display:none'><b>GONE2</b></div></td>\n    "
                 "<td style='width:120px'><span style='font-size:0'>ZERO</span></td>\n    "
                 "<td style='width:300px'></td>\n    <td class=o_data_cell><span id=t>Visible</span></td>\n    <td>after</td>")
        cur, tbox, _rb, lg, _mv = self._point(page_wide_row(cells))
        self.assert_beside_text(cur, tbox)
        self.assertEqual(lg["clicks"], [])

    def test_row_without_any_text_falls_back_to_the_element_box(self):
        # Documented fallback (old behaviour): beside the row → no room right → its left edge (gap/2).
        cells = HANDLE_TD + SELECTOR_TD + "<td style='width:900px'>  </td>\n    <td><input value='typed, not a text node'></td>"
        cur, _t, rbox, lg, _mv = self._point(page_wide_row(cells), text_sel=None)
        self.assertEqual(cur.x, ch.POINT_GAP / 2)
        self.assertAlmostEqual(cur.y, rbox["y"] + rbox["height"] / 2, delta=1)
        self.assertEqual(lg["clicks"], [])

    def test_wide_container_points_beside_its_centred_heading_text(self):
        html = (HEAD + NAV + OC + "<div style='height:200px'></div>"
                "<div id=col style='width:1500px;margin-left:100px;text-align:center;background:#eee'>"
                "<div class=heading style='height:40px;line-height:40px'>\n  <span id=t>In Progress</span>\n</div>"
                "<div style='height:300px'></div></div><div style='height:1500px'></div></div>" + LOGGER + MOVE_LOG + "</body></html>")
        cur, tbox, rbox, lg, _mv = self._point(html, sel="#col")
        self.assertGreater(tbox["x"], rbox["x"] + 400, "premise: centred text, far from the container's left edge")
        self.assert_beside_text(cur, tbox)
        self.assertEqual(lg["clicks"], [])

    def test_vertically_scrolled_wide_container_points_beside_the_first_visible_paragraph(self):
        # the four vertical clamps were unguarded (mutant V left this file green). A
        # 1400-px dialog body scrolled 200 px: p0..p4 sit above its visible part; their unclipped Range
        # rects would put the cursor above the scroller, beside text nobody sees.
        paras = "".join(f"<p id=p{i} style='margin:0;height:40px;line-height:40px'>Paragraph {i}</p>" for i in range(20))
        html = ("<html><body style='margin:0;font:16px sans-serif'><div style='height:200px'></div>"
                "<div id=dlg style='width:1400px;margin:0 auto;border:1px solid #888'>"
                "<div id=body style='height:200px;overflow-y:auto'>" + paras + "</div></div>" + LOGGER + "</body></html>")
        cur = self.load(html)
        self.ev("()=>{document.getElementById('body').scrollTop = 200}")
        self.run_(ch.point_at(self.page, self.page.locator("#body"), cur, hold=0, scroll=False, **FAST))
        body = self.run_(self.page.locator("#body").bounding_box())
        p4 = self.run_(self.page.locator("#p4").bounding_box())
        self.assertLessEqual(p4["y"] + p4["height"], body["y"], "premise: p0..p4 are scrolled above the visible part")
        self.assertGreaterEqual(cur.y, body["y"])
        self.assertLessEqual(cur.y, body["y"] + body["height"], f"cursor y {cur.y} outside the scroller {body}")
        hit = self.ev("([x,y])=>{const e=document.elementFromPoint(x,y); return e ? e.id || e.tagName : null;}", [cur.x, cur.y])
        self.assertEqual(hit, "p5", "beside the first VISIBLE paragraph")
        self.assertEqual(self.ev("()=>__clicks"), [], "point_at must not click")

    def test_row_below_the_fold_scrolled_then_pointed_beside_its_text(self):
        cells = HANDLE_TD + "<td style='width:700px'></td>\n    <td><span id=t>Far row</span></td>"
        cur, tbox, _rb, lg, _mv = self._point(page_wide_row(cells, top_px=2400))
        self.assertGreaterEqual(tbox["y"], 104, "scrolled into the visible page area first")
        self.assertLessEqual(tbox["y"] + tbox["height"], VH)
        self.assert_beside_text(cur, tbox)
        self.assertGreater(lg["wheels"], 0)
        self.assertEqual(lg["clicks"], [])

    # --- the text box must be the VISIBLE (clipped) part ----
    def test_odoo_text_truncate_first_cell_points_beside_the_visible_cell_end_fix_review_r1(self):
        # Odoo 17 truncates every list data cell (list_renderer.scss:80 text-truncate). The unclipped Range
        # rect spans the whole 676-px text → the cursor sat 534 px past the cell, on the "Draft" state cell.
        long = "[FURN_8888] Large Cabinet with a very long display name that Odoo cuts with an ellipsis in lists"
        cells = (SELECTOR_TD + "<td class=o_data_cell id=cell style='width:150px;overflow:hidden;"
                 "text-overflow:ellipsis;white-space:nowrap'><span id=t>" + long + "</span></td>\n    "
                 "<td class=o_data_cell id=qty style='width:100px'>5.00</td>\n    "
                 "<td class=o_data_cell id=state><span id=st>Draft</span></td>")
        cur, tbox, _rb, lg, _mv = self._point(page_wide_row(cells))
        cell = self.run_(self.page.locator("#cell").bounding_box())
        state = self.run_(self.page.locator("#state").bounding_box())
        right = cell["x"] + cell["width"]
        self.assertGreater(tbox["x"] + tbox["width"], state["x"], "premise: the full text runs past the state cell")
        self.assertGreater(cur.x, right - 0.5, f"cursor {cur.x:.1f} inside the truncated cell (ends {right:.1f})")
        self.assertLessEqual(cur.x - right, ch.POINT_GAP + 1, f"cursor {cur.x:.1f} far past the visible cell end {right:.1f}")
        self.assertLess(cur.x, state["x"], "the pointer is on another column's value")
        self.assertAlmostEqual(cur.y, tbox["y"] + tbox["height"] / 2, delta=1.5)
        self.assertEqual(lg["clicks"], [])

    def test_row_in_a_horizontally_scrolled_list_points_beside_the_first_ON_SCREEN_text(self):
        # scrollLeft=1500 put the cursor at x=-1186.7 (off-screen, elementFromPoint null).
        html = (HEAD + WIDE_CSS + NAV + OC + "<div style='height:300px'></div><div id=hs style='overflow-x:auto'>"
                "<table class=o_list_table style='width:4000px'><tbody><tr class=o_data_row id=row>"
                "<td style='width:100px'><span id=t0>Scrolled away</span></td><td style='width:1900px'></td>"
                "<td><span id=t>On screen</span></td></tr></tbody></table></div>"
                "<div style='height:1500px'></div></div>" + LOGGER + MOVE_LOG + "</body></html>")
        cur = self.load(html)
        self.ev("()=>{document.getElementById('hs').scrollLeft = 1500}")
        self.reset_logs()
        self.run_(ch.point_at(self.page, self.page.locator("#row"), cur, hold=0, scroll=False, **FAST))
        t0 = self.run_(self.page.locator("#t0").bounding_box())
        tbox = self.run_(self.page.locator("#t").bounding_box())
        self.assertLess(t0["x"] + t0["width"], 0, "premise: the first text is scrolled off-screen")
        self.assert_beside_text(cur, tbox)
        self.assertEqual(self.logs()["clicks"], [])

    def test_window_scrolled_sideways_skips_text_left_of_the_viewport(self):
        # Same shape with the WINDOW as the horizontal scroller: no overflow ancestor clips, only the
        # viewport clamp does.
        html = ("<html><head>" + WIDE_CSS + "</head><body style='margin:0;font:16px sans-serif'>"
                "<div style='height:300px'></div><table class=o_list_table style='width:4000px'><tbody>"
                "<tr class=o_data_row id=row><td style='width:100px'><span id=t0>Scrolled away</span></td>"
                "<td style='width:1900px'></td><td><span id=t>On screen</span></td></tr></tbody></table>"
                + LOGGER + "</body></html>")
        cur = self.load(html)
        self.ev("()=>{window.scrollTo(1500, 0)}")
        self.run_(ch.point_at(self.page, self.page.locator("#row"), cur, hold=0, scroll=False, **FAST))
        t0 = self.run_(self.page.locator("#t0").bounding_box())
        tbox = self.run_(self.page.locator("#t").bounding_box())
        self.assertEqual(self.ev("()=>scrollX"), 1500, "premise: the window is scrolled sideways")
        self.assertLess(t0["x"] + t0["width"], 0, "premise: the first text is left of the viewport")
        self.assert_beside_text(cur, tbox)

    def test_scrolled_row_with_no_on_screen_text_falls_back_on_screen(self):
        html = (HEAD + WIDE_CSS + NAV + OC + "<div style='height:300px'></div><div id=hs style='overflow-x:auto'>"
                "<table class=o_list_table style='width:4000px'><tbody><tr class=o_data_row id=row>"
                "<td style='width:100px'><span id=t0>Scrolled away</span></td><td></td></tr></tbody></table></div>"
                "<div style='height:1500px'></div></div>" + LOGGER + "</body></html>")
        cur = self.load(html)
        self.ev("()=>{document.getElementById('hs').scrollLeft = 1500}")
        self.run_(ch.point_at(self.page, self.page.locator("#row"), cur, hold=0, scroll=False, **FAST))
        self.assertEqual(cur.x, ch.POINT_GAP / 2, "the old element-box fallback, on screen")
        self.assertTrue(self.ev("([x,y])=>!!document.elementFromPoint(x,y)", [cur.x, cur.y]), "pointer off-screen")

    def test_wide_row_inside_a_css_scaled_iframe_fix_review_r1(self):
        # scale(0.5) put the cursor at 1305.8 while the text ended at 694.9.
        doc = ("<html><head>" + WIDE_CSS + "</head><body style='margin:0;font:16px sans-serif'>"
               "<table class=o_list_table><tbody><tr class=o_data_row id=row>" + HANDLE_TD
               + "<td style='width:1000px'></td><td><span id=t>Inside scaled</span></td></tr></tbody></table>"
               "</body></html>")
        cur, tbox, rbox, lg, _mv = self._point(
            page_iframe("width:2000px;height:200px;transform:scale(0.5);transform-origin:0 0", doc),
            frame="#f", scroll=False)
        self.assertGreater(rbox["width"], VW / 2, "premise: the row is wider than half the page after scaling")
        self.assertAlmostEqual(rbox["width"], 1000, delta=2, msg="premise: scale 0.5 applies")
        self.assert_beside_text(cur, tbox)
        self.assertEqual(lg["clicks"], [])

    def test_wide_row_inside_an_iframe_offset_lands_in_main_page_coordinates(self):
        doc = ("<html><head>" + WIDE_CSS + "</head><body style='margin:0;font:16px sans-serif'>"
               "<div style='height:40px'></div><table class=o_list_table><tbody><tr class=o_data_row id=row>"
               + HANDLE_TD + "<td style='width:500px'></td><td><span id=t>Inside frame</span></td></tr></tbody></table>"
               "</body></html>")
        cur, tbox, rbox, lg, _mv = self._point(page_iframe("width:1450px;height:300px", doc), frame="#f", scroll=False)
        self.assertGreater(rbox["width"], VW / 2, "premise: the in-frame row is wider than half the page")
        self.assert_beside_text(cur, tbox)
        self.assertEqual(lg["clicks"], [])


# Odoo 17 statusbar: the CURRENT stage is a disabled .btn and Bootstrap 5.1.3 gives
# `.btn:disabled { pointer-events: none }` — elementFromPoint over it returns its parent.
BOOT_BTN = ("<style>.btn{display:inline-block;padding:6px 12px;border:1px solid #888;background:#eee;"
            "cursor:pointer;font:16px sans-serif}.btn:disabled{pointer-events:none;opacity:.65}</style>")


def page_stage(top_px: int, left_px: int = 600) -> str:
    return (HEAD + BOOT_BTN + NAV + OC + f"<div style='height:{top_px}px'></div>"
            f"<div class=o_statusbar_status id=sb style='margin-left:{left_px}px'>"
            "<button class='btn o_arrow_button' id=st_draft disabled>Draft</button>"
            "<button class='btn o_arrow_button o_arrow_button_current' id=st_done disabled aria-checked=true>Done</button>"
            "<button class='btn o_arrow_button' id=st_cancel>Cancelled</button></div>"
            "<div style='height:2500px'></div></div>" + LOGGER + MOVE_LOG + "</body></html>")


# A fixed bar that does NOT contain the stage, parked over the middle of the page area: scrolling
# moves the stage but never out from under the bar's band when the bar is mid-screen.
FIXED_BAR = ("<div id=toast style='position:fixed;left:0;right:0;top:400px;height:200px;"
             "background:#fcc;z-index:6'>toast</div>")


class TestPointAtPointerEventsNone(RealChromeCase):
    """point_at raised "Không đưa được phần tử
    vào vị trí click được" on a `pointer-events: none` target (Odoo 17 current statusbar stage)
    because the click hit-test never matched it. point_at now scrolls with pointing=True (an
    ANCESTOR under the point counts); click paths keep the strict hit-test."""

    def _point(self, html, sel="#st_done", cursor=(960.0, 600.0)):
        cur = self.load(html, cursor=cursor)
        self.reset_logs()
        err = None
        try:
            self.run_(ch.point_at(self.page, self.page.locator(sel), cur, hold=0, **FAST))
        except RuntimeError as e:
            err = e
        box = self.run_(self.page.locator(sel).bounding_box())
        return err, cur, box, self.logs()

    def test_premise_stage_is_never_element_from_point(self):
        self.load(page_stage(200))
        got = self.ev("()=>{const b=document.getElementById('st_done').getBoundingClientRect();"
                      " return document.elementFromPoint(b.left+b.width/2, b.top+b.height/2).id;}")
        self.assertEqual(got, "sb", "premise: pointer-events:none → its parent is hit")

    def test_visible_disabled_stage_is_pointed_at_without_click(self):
        err, cur, box, lg = self._point(page_stage(200))
        self.assertIsNone(err, f"point_at raised on a pointer-events:none stage: {err}")
        right = box["x"] + box["width"]
        self.assertGreater(cur.x, right)
        self.assertLessEqual(cur.x - right, ch.POINT_GAP + 1)
        self.assertAlmostEqual(cur.y, box["y"] + box["height"] / 2, delta=1)
        self.assertEqual(lg["wheels"], 0, "already visible: no scroll")
        self.assertEqual(lg["clicks"], [])

    def test_disabled_stage_below_the_fold_scrolls_then_points(self):
        err, cur, box, lg = self._point(page_stage(2400))
        self.assertIsNone(err, f"point_at raised on a pointer-events:none stage: {err}")
        self.assertGreater(lg["wheels"], 0)
        self.assertGreaterEqual(box["y"], 104)
        self.assertLessEqual(box["y"] + box["height"], VH)
        self.assertLessEqual(cur.x - (box["x"] + box["width"]), ch.POINT_GAP + 1)
        self.assertEqual(lg["clicks"], [])

    def test_disabled_stage_under_control_panel_is_scrolled_out_from_under(self):
        # The control panel (fixed, not an ancestor) covers the stage: pointing mode must NOT
        # accept the panel as a hit — the stage is scrolled down into the free area first.
        cur = self.load(page_stage(600))
        self.ev("()=>{const oc=document.getElementById('oc'); const b=document.getElementById('st_done');"
                " oc.scrollTop += b.getBoundingClientRect().top - 60;}")   # stage top ≈ 60 px: under #cp
        premise = self.ev("()=>{const b=document.getElementById('st_done').getBoundingClientRect();"
                          " return document.getElementById('cp').contains("
                          "document.elementFromPoint(b.left+b.width/2, b.top+b.height/2));}")
        self.assertTrue(premise, "premise: the stage sits under the control panel")
        self.reset_logs()
        self.run_(ch.point_at(self.page, self.page.locator("#st_done"), cur, hold=0, **FAST))
        box = self.run_(self.page.locator("#st_done").bounding_box())
        lg = self.logs()
        self.assertGreater(lg["wheels"], 0, "scrolled (up) out from under the panel")
        self.assertGreaterEqual(box["y"], 104, "the stage is below the control panel now")
        self.assertEqual(lg["clicks"], [])

    def test_disabled_stage_covered_by_a_fixed_bar_mid_screen_raises_never_points(self):
        # Measured 2026-10-21 (pinned): a non-ancestor fixed bar over the stage that scrolling
        # cannot clear → RuntimeError, the cursor is not parked beside a hidden value.
        html = page_stage(1200).replace("</body>", FIXED_BAR + "</body>")
        cur = self.load(html)
        self.ev("()=>{const oc=document.getElementById('oc'); const b=document.getElementById('st_done');"
                " oc.scrollTop += b.getBoundingClientRect().top - 480;}")   # stage centred in the bar
        premise = self.ev("()=>{const b=document.getElementById('st_done').getBoundingClientRect();"
                          " return document.elementFromPoint(b.left+b.width/2, b.top+b.height/2).id;}")
        self.assertEqual(premise, "toast")
        self.reset_logs()
        with self.assertRaisesRegex(RuntimeError, "Không đưa được phần tử"):
            self.run_(ch.point_at(self.page, self.page.locator("#st_done"), cur, hold=0, **FAST))
        self.assertEqual(self.logs()["clicks"], [])

    def test_click_box_on_pointer_events_none_still_refuses(self):
        # click paths keep the strict hit-test (pointing=False): a click on a pointer-events:none
        # element lands on its parent, so click_box raises instead — today's behaviour, pinned.
        cur = self.load(page_stage(200))
        self.reset_logs()
        with self.assertRaisesRegex(RuntimeError, "Không đưa được phần tử"):
            self.run_(ch.click_box(self.page, self.page.locator("#st_done"), cur, **FAST))
        self.assertEqual(self.logs()["clicks"], [])

    def test_click_box_on_enabled_stage_next_to_it_still_clicks(self):
        cur = self.load(page_stage(200))
        self.reset_logs()
        self.run_(ch.click_box(self.page, self.page.locator("#st_cancel"), cur, **FAST))
        self.assertEqual(self.logs()["clicks"], ["st_cancel"])


DOC_STAGE = ("<html><head>" + BOOT_BTN + "</head><body style='margin:0;background:#ffe'>"
             "<div id=sb style='position:absolute;left:60px;top:60px'>"
             "<button class=btn id=st_done disabled>Done</button><button class=btn id=st_cancel>Cancelled</button></div>"
             "<span id=val style='position:absolute;left:60px;top:150px;font-size:24px'>42.0</span>"
             + INNER_LOG + "</body></html>")


class TestPointAtPointerEventsNoneInIframe(RealChromeCase):
    """DOCUMENTED LIMITATION (REFERENCE scripts table): inside an
    iframe, bring_into_view's accept() still uses the strict click hit-test (_hits_chain), so
    point_at on a pointer-events:none target there raises — use scroll=False. Pinned on purpose:
    a fix that makes the scroll path accept it must flip the first test consciously."""

    def _setup(self):
        cur = self.load(page_iframe("height:300px", DOC_STAGE))
        self.fl = self.page.frame_locator("#f")
        self.run_(self.fl.locator("#st_done").wait_for(timeout=5000))
        self.reset_logs()
        return cur

    def _inner_clicks(self):
        return self.run_(self.fl.locator("body").evaluate("()=>window.__in"))

    def test_documented_scroll_path_raises_and_never_clicks(self):
        cur = self._setup()
        with self.assertRaisesRegex(RuntimeError, "Không đưa được phần tử"):
            self.run_(ch.point_at(self.page, self.fl.locator("#st_done"), cur, hold=0, **FAST))
        self.assertEqual(self.logs()["clicks"], [])
        self.assertEqual(self._inner_clicks(), [])

    def test_scroll_false_points_beside_the_iframe_stage(self):
        cur = self._setup()
        loc = self.fl.locator("#st_done")
        self.run_(ch.point_at(self.page, loc, cur, hold=0, scroll=False, **FAST))
        box = self.run_(loc.bounding_box())
        right = box["x"] + box["width"]
        self.assertGreater(cur.x, right)
        self.assertLessEqual(cur.x - right, ch.POINT_GAP + 1)
        self.assertAlmostEqual(cur.y, box["y"] + box["height"] / 2, delta=1)
        self.assertEqual(self.logs()["clicks"], [])
        self.assertEqual(self._inner_clicks(), [])

    def test_enabled_value_in_iframe_scroll_path_works(self):
        cur = self._setup()
        loc = self.fl.locator("#val")
        self.run_(ch.point_at(self.page, loc, cur, hold=0, **FAST))
        box = self.run_(loc.bounding_box())
        self.assertLessEqual(cur.x - (box["x"] + box["width"]), ch.POINT_GAP + 1)
        self.assertGreater(cur.x, box["x"] + box["width"])
        self.assertEqual(self._inner_clicks(), [])


def page_options(items) -> str:
    """AutoComplete-like list; items = (id, text, extra_style)."""
    lis = "".join(f"<li><a id={i} style='display:block;height:30px;{st}'>{t}</a></li>" for i, t, st in items)
    return (HEAD + NAV + OC + "<div style='height:200px'></div>"
            f"<ul style='margin-left:300px;width:400px;list-style:none'>{lis}</ul>"
            "<div style='height:600px'></div></div>" + LOGGER + "</body></html>")


HIDDEN = "display:none"


class TestClickTextExactMatch(RealChromeCase):
    """_FIND_LEAF_TEXT_JS: an exact leaf match beats an EARLIER substring match (in a real take
    "Fabric Cutting (old)" listed before "Cutting" picked the wrong record)."""

    def _click(self, items, text="Cutting"):
        cur = self.load(page_options(items))
        self.reset_logs()
        self.run_(ch.click_text(self.page, text, cur, timeout=1.0, **FAST))
        return self.logs()["clicks"]

    def test_exact_match_wins_over_earlier_substring(self):
        clicks = self._click([("o_old", "Fabric Cutting (old)", ""), ("o_cut", "Cutting", "")])
        self.assertEqual(clicks, ["o_cut"])

    def test_exact_match_case_insensitive_and_nbsp_wins_too(self):
        clicks = self._click([("o_old", "Fabric Cutting (old)", ""), ("o_cut", "&nbsp;CUTTING&nbsp;", "")])
        self.assertEqual(clicks, ["o_cut"])

    def test_only_substring_candidates_first_one_is_clicked(self):
        clicks = self._click([("o_old", "Fabric Cutting (old)", ""), ("o_room", "Cutting Room", "")])
        self.assertEqual(clicks, ["o_old"])

    def test_hidden_exact_match_falls_back_to_visible_substring(self):
        clicks = self._click([("o_hidden", "Cutting", HIDDEN), ("o_old", "Fabric Cutting (old)", ""),
                              ("o_room", "Cutting Room", "")])
        self.assertEqual(clicks, ["o_old"])

    def test_hidden_exact_then_visible_exact_later_still_wins(self):
        clicks = self._click([("o_old", "Fabric Cutting (old)", ""), ("o_hidden", "Cutting", "width:0;height:0;overflow:hidden;padding:0"),
                              ("o_cut", "Cutting", "")])
        self.assertEqual(clicks, ["o_cut"])

    def test_hidden_substring_is_never_the_fallback(self):
        # The substring fallback keeps the size check: a hidden earlier substring is skipped.
        clicks = self._click([("o_hid", "Fabric Cutting (old)", HIDDEN), ("o_room", "Cutting Room", "")])
        self.assertEqual(clicks, ["o_room"])

    def test_exact_match_is_page_wide_even_off_screen_documented(self):
        # DOCUMENTED behaviour (REFERENCE.md §6, "Exact is page-wide"): an exact leaf ANYWHERE on
        # the page — here a chatter tracking value far below the fold — beats a partial match in
        # the open dropdown listed earlier in the DOM. Pinned so a change to this rule is deliberate.
        html = (HEAD + NAV + OC + "<div style='height:150px'></div>"
                "<ul id=dd style='margin-left:300px;width:400px;list-style:none'>"
                "<li><a id=item style='display:block;height:30px'>Cutting DA32-00006A</a></li></ul>"
                "<div id=chatter style='margin-top:2500px'><span id=track>Cutting</span></div>"
                "<div style='height:600px'></div></div>" + LOGGER + "</body></html>")
        cur = self.load(html)
        found = self.ev(f"(args)=>{{const f={ch._FIND_LEAF_TEXT_JS.strip()}; const n=f(args); return n && n.id;}}",
                        ["Cutting", False])
        self.assertEqual(found, "track", "the off-screen exact leaf is the target, not the dropdown item")
        self.reset_logs()
        self.run_(ch.click_text(self.page, "Cutting", cur, timeout=1.0, **FAST))
        lg = self.logs()
        self.assertEqual(lg["clicks"], ["track"])
        self.assertGreater(lg["wheels"], 0, "it scrolled away from the dropdown to reach it")


class TestScrollThrough(RealChromeCase):
    def test_nested_scroller_reaches_bottom_of_outer_scroller(self):
        cur = self.load(PAGE_NESTED)
        self.run_(ch.scroll_through(self.page, cur, pause=0.05, hold=0))
        st, mx = self.ev("()=>{const oc=document.getElementById('oc'); return [oc.scrollTop, oc.scrollHeight-oc.clientHeight];}")
        self.assertLess(abs(st - mx), 2, f"stopped at {st} of {mx}")


async def _start():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    errors = []
    for exe in (None,) + CHROME_FALLBACKS:
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
    t0 = time.monotonic()
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
    print(f"real-chrome file time: {time.monotonic() - t0:.1f}s", file=sys.stderr)
    return 0 if prog.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
