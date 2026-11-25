#!/usr/bin/env python3
"""Real-Chromium tests for the in-page behaviour the twin tests cannot see:

- tour_helpers.mask_text_js: the init-script JS really masks text nodes (incl. later redraws, several
  matches per node, quotes/backslashes in pattern + replacement) and leaves <input> values alone;
  the MutationObserver masks an added / rewritten node in the SAME task (no interval needed), a
  replacement that matches the pattern does not loop, observe=False and the interval safety net work.
- tour_helpers.mask_input_overlay_js: an overlay shows the masked value over an <input>/<textarea>
  while `.value` stays RAW (Odoo would save a rewritten value into the record); it follows scroll /
  resize, goes away with the value / the input, never catches the mouse.
- tour_helpers.assert_form_clean: the real selector against Odoo's Save/Discard indicator markup.
- click_helpers.press_button: an Odoo error dialog (`.o_error_dialog`) shown instead of a toast
  raises at once with the dialog text.
- click_helpers.exact_text_re through Playwright's `get_by_text(regex)` / `filter(has_text=regex)`:
  a label whose DOM text differs in case from the shown label ("Test call" vs "Test Call") matches,
  a longer label ("Accounting" for "Account") does not.
- click_helpers.press_button end to end: waits for the OLD toast to leave, clicks only the right
  button, waits for the NEW toast, raises when no toast comes, reports (not raises) a stuck old toast.

Availability: no `playwright` package or no launchable Chromium (bundled, /usr/bin/google-chrome,
macOS Chrome) -> prints one `SKIP-FILE: <reason>` line and exits 0 (run-tests.sh protocol), the same
gate as test_click_helpers_real_chrome.py. Chrome is launched ONCE per file; each test gets a fresh page.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_tour_helpers_real_chrome.py
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import os
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE.parent / file)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ch = _load("click_helpers_tour_real_mod", "click_helpers.py")
th = _load("tour_helpers_real_mod", "tour_helpers.py")

CHROME_FALLBACKS = ("/usr/bin/google-chrome",
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LOOP: asyncio.AbstractEventLoop | None = None
PW = None
BROWSER = None
ORIGIN = "http://demo.test"
ACCOUNT = "5500123412341234"          # bank-account style number: 8 digits + last 4 kept


class RealChromeCase(unittest.TestCase):
    def setUp(self):
        self.page = self.run_(BROWSER.new_page(viewport={"width": 1280, "height": 800}))
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def tearDown(self):
        # bounded: a page a buggy mask script sent into an endless loop must fail ITS test, not hang the file
        try:
            self.run_(asyncio.wait_for(self.page.close(), 15))
        except Exception:  # noqa: BLE001 — the test already failed for the real reason
            pass

    @staticmethod
    def run_(coro):
        return LOOP.run_until_complete(coro)

    def ev(self, js: str, arg=None, timeout: float = 10.0):
        """page.evaluate with a deadline: a hung page raises TimeoutError instead of blocking forever."""
        return self.run_(asyncio.wait_for(self.page.evaluate(js, arg), timeout))

    def wait_fn(self, expression: str, arg=None, timeout_ms: int = 3000, what: str = ""):
        """Wait until the JS predicate is truthy (polled every frame); fail with `what` otherwise."""
        try:
            self.run_(self.page.wait_for_function(expression, arg=arg, timeout=timeout_ms))
        except Exception:
            self.fail(f"timed out waiting for: {what or expression}")

    def serve(self, html: str, slow_script: float = 0.0, init_script: str | None = None, fake_clock: bool = False):
        """Open `html` at ORIGIN/ (init script installed first, like add_init_script in a demo).
        slow_script > 0: the head loads /slow.js, which the server answers after that many seconds —
        the document has NO <body> yet while it waits (timers still fire).
        fake_clock: Playwright's fake timers (installed BEFORE the init script, so its setInterval is
        the fake one) — the test then moves time itself with page.clock.run_for."""
        async def go():
            if fake_clock:
                await self.page.clock.install(time=0)
            async def handler(route):
                if route.request.url.endswith("/slow.js"):
                    await asyncio.sleep(slow_script)
                    await route.fulfill(status=200, content_type="application/javascript", body="")
                else:
                    await route.fulfill(status=200, content_type="text/html", body=html)

            await self.page.route(f"{ORIGIN}/**", handler)
            if init_script:
                await self.page.add_init_script(script=init_script)
            await self.page.goto(f"{ORIGIN}/", wait_until="load")
        self.run_(go())

    def text(self, selector: str) -> str:
        return self.run_(self.page.evaluate("(s) => document.querySelector(s).textContent", selector))

    def wait_text(self, selector: str, expected: str, timeout_ms: int = 3000):
        """Wait until the element's text equals `expected` (the mask runs on an interval)."""
        try:
            self.run_(self.page.wait_for_function(
                "([s, t]) => document.querySelector(s).textContent === t", arg=[selector, expected], timeout=timeout_ms))
        except Exception:
            self.fail(f"{selector} text is {self.text(selector)!r}, expected {expected!r}")


class TestMaskTextJsInBrowser(RealChromeCase):
    PATTERN = r"\b5500\d{8}(\d{4})\b"     # 16-digit account: prefix 5500, 8 hidden digits, last 4 kept

    def masked(self, html, pattern=None, replacement="********$1", observe=True, **kw):
        js = th.mask_text_js(pattern if pattern is not None else self.PATTERN, replacement,
                             interval_ms=20, observe=observe)
        self.serve(html, init_script=js, **kw)

    def test_masks_an_existing_text_node_keeping_the_group(self):
        self.masked(f"<html><body><div id=a>Acct {ACCOUNT} ok</div></body></html>")
        self.wait_text("#a", "Acct ********1234 ok")
        self.assertEqual(self.errors, [])

    def test_text_redrawn_later_is_masked_too(self):
        # Odoo redraws the list after every refresh: new nodes AND a node rewritten with the raw value.
        self.masked("<html><body><div id=a>x</div><div id=list></div></body></html>")
        self.run_(self.page.evaluate(
            "(n) => { const d = document.createElement('p'); d.id = 'late'; d.textContent = 'No. ' + n;"
            " document.getElementById('list').appendChild(d); }", ACCOUNT))
        self.wait_text("#late", "No. ********1234")
        self.run_(self.page.evaluate(
            "(n) => { document.getElementById('a').firstChild.nodeValue = n; }", ACCOUNT))
        self.wait_text("#a", "********1234")

    def test_every_match_in_one_text_node_is_masked(self):
        self.masked(f"<html><body><div id=a>{ACCOUNT} / 5500999988887777</div></body></html>")
        self.wait_text("#a", "********1234 / ********7777")

    def test_one_tick_masks_every_match_and_ticks_come_every_interval_ms(self):
        # With a fake clock: ONE tick must leave no unmasked match behind (a regex without the g flag
        # masks one match per tick, so a second number stays readable for another interval), and the
        # tick period is the interval_ms asked for (250 here, not the default 100). observe=False: the
        # MutationObserver would mask the rewritten text at once and hide the interval's behaviour.
        js = th.mask_text_js(self.PATTERN, "********$1", interval_ms=250, observe=False)
        self.serve("<html><body><div id=a></div></body></html>", init_script=js, fake_clock=True)
        self.run_(self.page.clock.pause_at(1000))               # a tick may fire at the jump: nothing to mask yet
        self.run_(self.page.evaluate(
            "() => { document.getElementById('a').textContent = '5500123412341234 / 5500999988887777'; }"))
        raw = f"{ACCOUNT} / 5500999988887777"
        self.run_(self.page.clock.run_for(150))
        self.assertEqual(self.text("#a"), raw, "no tick may fire before interval_ms has passed")
        self.run_(self.page.clock.run_for(150))                 # crosses the 250 ms boundary exactly once
        self.assertEqual(self.text("#a"), "********1234 / ********7777", "one tick must mask EVERY match")

    def test_non_matching_text_is_left_alone(self):
        self.masked("<html><body><div id=a>Balance 1.000.000 for 55001234</div><div id=b>done</div></body></html>")
        self.wait_text("#b", "done")
        time.sleep(0.15)            # several intervals later the text is still untouched
        self.assertEqual(self.text("#a"), "Balance 1.000.000 for 55001234")

    def test_input_value_is_not_masked(self):
        self.masked(f"<html><body><input id=i value='{ACCOUNT}'><div id=a>{ACCOUNT}</div></body></html>")
        self.wait_text("#a", "********1234")
        self.assertEqual(self.run_(self.page.evaluate("() => document.getElementById('i').value")), ACCOUNT)

    def test_quotes_backslashes_and_newlines_in_pattern_and_replacement_survive(self):
        # json.dumps embedding: a quote / backslash in the arguments must reach the regex and the
        # replacement byte for byte (a naive '%s' embedding is a syntax error or a different regex).
        rep = "it's \"x\" \\ end"
        self.masked('<html><body><div id=a>say "secret" now</div></body></html>', pattern=r'"secret"', replacement=rep)
        self.wait_text("#a", f"say {rep} now")
        self.assertEqual(self.errors, [])

    def test_a_dollar_group_and_a_literal_dollar_follow_js_replace_rules(self):
        # The replacement must NOT match the pattern again ("$2-$1" would: the interval then flips the
        # text back and forth every tick and the wait races it) — ":" keeps the result stable.
        self.masked("<html><body><div id=a>id 12-34</div></body></html>", pattern=r"(\d+)-(\d+)", replacement="$2:$1 $$")
        self.wait_text("#a", "id 34:12 $")

    def test_the_mask_survives_a_document_with_no_body_yet(self):
        # The init script's interval fires while the head still waits for a slow script: no <body>.
        # The guard must keep that tick silent (no uncaught TypeError), and masking must still work after.
        self.masked(f"<html><head><script src='/slow.js'></script></head><body><div id=a>{ACCOUNT}</div></body></html>",
                    slow_script=0.4)
        self.wait_text("#a", "********1234")
        self.assertEqual(self.errors, [], "an interval tick before <body> exists threw")


class TestMaskTextJsObserverInBrowser(RealChromeCase):
    """The MutationObserver half of mask_text_js. The interval is set to ~2.8 hours (HUGE), so any
    masking seen here came from the observer; every evaluate() reads the DOM right after a microtask,
    i.e. BEFORE the next paint — the point of the observer (the interval alone showed the raw value)."""

    PATTERN = r"\b5500\d{8}(\d{4})\b"
    HUGE = 10_000_000
    BASE = "<html><body><div id=a>x</div><div id=list></div></body></html>"

    def open(self, html=None, observe=True, prefix="", pattern=None, replacement="********$1", interval_ms=None):
        js = prefix + th.mask_text_js(pattern if pattern is not None else self.PATTERN, replacement,
                                      interval_ms=interval_ms or self.HUGE, observe=observe)
        self.serve(html or self.BASE, init_script=js)

    APPEND = ("async (n) => { const d = document.createElement('p'); d.id = 'late'; d.textContent = 'No. ' + n;"
              " document.getElementById('list').appendChild(d); await Promise.resolve(); return d.textContent; }")

    def test_a_node_appended_later_is_masked_in_the_same_task_without_any_interval(self):
        self.open()
        self.assertEqual(self.ev(self.APPEND, ACCOUNT), "No. ********1234")

    def test_text_deep_inside_an_appended_subtree_is_masked_every_match(self):
        self.open()
        got = self.ev("async (n) => { const d = document.createElement('div'); d.innerHTML ="
                      " '<section><span id=s1>' + n + '</span><b><i id=s2>5500999988887777 and ' + n + '</i></b></section>';"
                      " document.getElementById('list').appendChild(d); await Promise.resolve();"
                      " return [document.getElementById('s1').textContent, document.getElementById('s2').textContent]; }", ACCOUNT)
        self.assertEqual(got, ["********1234", "********7777 and ********1234"])

    def test_a_text_node_rewritten_later_is_masked_in_the_same_task(self):
        # characterData: Odoo updates a cell with `node.data = ...` / `node.nodeValue = ...`
        self.open()
        got = self.ev("async (n) => { const t = document.getElementById('a').firstChild; t.nodeValue = n; await Promise.resolve();"
                      " const viaNodeValue = t.nodeValue; t.data = 'again ' + n; await Promise.resolve(); return [viaNodeValue, t.data]; }",
                      ACCOUNT)
        self.assertEqual(got, ["********1234", "again ********1234"])

    def test_an_element_whose_children_are_replaced_is_masked_in_the_same_task(self):
        self.open()
        got = self.ev("async (n) => { const a = document.getElementById('a'); a.textContent = n; await Promise.resolve();"
                      " return a.textContent; }", ACCOUNT)
        self.assertEqual(got, "********1234")

    def test_text_parsed_from_the_initial_html_is_masked_without_any_interval(self):
        self.open(f"<html><body><div id=a>Acct {ACCOUNT} ok</div></body></html>")
        self.wait_text("#a", "Acct ********1234 ok")

    def test_an_input_value_is_never_touched_by_the_observer(self):
        self.open()
        got = self.ev("async (n) => { const i = document.createElement('input'); i.id = 'inp'; i.value = n;"
                      " document.getElementById('list').appendChild(i); await new Promise(r => setTimeout(r, 120));"
                      " return i.value; }", ACCOUNT)
        self.assertEqual(got, ACCOUNT)

    def test_a_replacement_that_matches_the_pattern_does_not_loop_and_the_page_stays_responsive(self):
        # pattern 'a' -> 'aa': without obs.takeRecords() our own write is observed again, forever, in
        # microtasks (the text doubles each round) and the page never returns to the event loop.
        self.open(html="<html><body><div id=a>a</div><div id=list></div></body></html>", pattern="a", replacement="aa")
        # Text parsed from the initial HTML is reported once per ANCESTOR record of the same batch
        # (html, body, div, text: each maskTree re-walks the already masked text), so a non-idempotent
        # replacement like this one grows to 2^k — bounded, never endless. Only the bound is asserted.
        time.sleep(0.3)
        parsed = self.text("#a")
        self.assertTrue(2 <= len(parsed) <= 64 and set(parsed) == {"a"}, f"parsed text {parsed!r} must be masked and bounded")
        got = self.ev("async () => { const d = document.createElement('p'); d.textContent = 'a'; document.getElementById('list').appendChild(d);"
                      " await new Promise(r => setTimeout(r, 200)); const t = d.firstChild; t.nodeValue = 'a';"
                      " await new Promise(r => setTimeout(r, 200)); return [d.textContent.length, t.nodeValue]; }", timeout=8)
        self.assertEqual(got, [2, "aa"], "each rewrite masks exactly once")
        self.assertEqual(self.ev("() => 1 + 1", timeout=3), 2, "the page is still responsive")
        self.assertEqual(self.text("#a"), parsed, "stable: nothing keeps rewriting the parsed text")

    def test_observe_false_masks_through_the_interval_only(self):
        self.open(observe=False, interval_ms=300)
        raw = self.ev(self.APPEND, ACCOUNT)
        self.assertEqual(raw, f"No. {ACCOUNT}", "no observer: the raw value is still there after a microtask")
        self.wait_text("#late", "No. ********1234", timeout_ms=3000)

    def test_observe_false_with_a_huge_interval_never_masks(self):
        self.open(observe=False)
        self.ev(self.APPEND, ACCOUNT)
        time.sleep(0.3)
        self.assertEqual(self.text("#late"), f"No. {ACCOUNT}")

    def test_the_interval_stays_a_safety_net_when_the_observer_never_fires(self):
        dead = "window.MutationObserver = class { observe() {} takeRecords() { return []; } disconnect() {} };"
        self.open(prefix=dead, interval_ms=20)
        self.ev(self.APPEND, ACCOUNT)
        self.wait_text("#late", "No. ********1234")

    def test_quotes_backslashes_and_dollars_survive_the_observer_path(self):
        rep = "it's \"x\" \\ end $$"
        self.open(html="<html><body><div id=a>x</div><div id=list></div></body></html>", pattern=r'"secret"', replacement=rep)
        got = self.ev("async () => { const d = document.createElement('p'); d.textContent = 'say \"secret\" now';"
                      " document.getElementById('list').appendChild(d); await Promise.resolve(); return d.textContent; }")
        self.assertEqual(got, "say it's \"x\" \\ end $ now")
        self.assertEqual(self.errors, [])

    def test_no_script_error_while_the_document_has_no_body_yet(self):
        self.open(html=f"<html><head><script src='/slow.js'></script></head><body><div id=a>{ACCOUNT}</div></body></html>")
        self.assertEqual(self.errors, [])
        self.wait_text("#a", "********1234")


class TestMaskInputOverlayInBrowser(RealChromeCase):
    PATTERN = r"\b5500\d{8}(\d{4})\b"
    HUGE = 10_000_000
    # Spy on every programmatic write to an input/textarea value (typing and the HTML attribute are not writes).
    SPY = ("window.__valueWrites = 0;"
           "for (const C of [HTMLInputElement, HTMLTextAreaElement]) {"
           " const d = Object.getOwnPropertyDescriptor(C.prototype, 'value');"
           " Object.defineProperty(C.prototype, 'value', {configurable: true, enumerable: d.enumerable, get: d.get,"
           "   set(v) { window.__valueWrites++; d.set.call(this, v); }}); }")
    BOX = "position:absolute;left:40px;width:220px;height:30px;font:16px monospace;border:1px solid #888;padding:2px 4px;"

    def open(self, html, interval_ms=20, spy=False, pattern=None, replacement="********$1", **kw):
        extra = {k: kw.pop(k) for k in ("selector",) if k in kw}
        js = th.mask_input_overlay_js(pattern if pattern is not None else self.PATTERN, replacement,
                                      interval_ms=interval_ms, **extra)
        self.serve(html, init_script=(self.SPY if spy else "") + js, **kw)

    def overlays(self):
        return self.ev("() => Array.from(document.querySelectorAll('[data-demo-mask]')).map((o) => {"
                       " const r = o.getBoundingClientRect(), cs = getComputedStyle(o);"
                       " return {text: o.textContent, left: r.left, top: r.top, width: r.width, height: r.height,"
                       "   pe: cs.pointerEvents, pos: cs.position, z: cs.zIndex, bg: cs.backgroundColor}; })")

    def rect(self, selector):
        return self.ev("(s) => { const r = document.querySelector(s).getBoundingClientRect();"
                       " return {left: r.left, top: r.top, width: r.width, height: r.height}; }", selector)

    def wait_overlays(self, n, timeout_ms=3000):
        self.wait_fn("(n) => document.querySelectorAll('[data-demo-mask]').length === n", arg=n, timeout_ms=timeout_ms,
                     what=f"{n} overlay(s); have {len(self.overlays())}")

    def wait_covering(self, selector, timeout_ms=3000):
        """Wait until the (single) overlay sits exactly on `selector`'s rect."""
        self.wait_fn("(s) => { const o = document.querySelector('[data-demo-mask]'), i = document.querySelector(s);"
                     " if (!o) return false; const a = o.getBoundingClientRect(), b = i.getBoundingClientRect();"
                     " return Math.abs(a.left - b.left) < 1 && Math.abs(a.top - b.top) < 1; }",
                     arg=selector, timeout_ms=timeout_ms, what=f"overlay to sit on {selector}")

    def assertCovers(self, ov, rect):
        for k in ("left", "top", "width", "height"):
            self.assertAlmostEqual(ov[k], rect[k], delta=1.0, msg=f"overlay {k} {ov[k]} vs input {rect[k]}")

    def one(self):
        (ov,) = self.overlays()
        return ov

    def page_with(self, body, style=""):
        return f"<html><body style='margin:0;{style}'>{body}</body></html>"

    def input_html(self, value=ACCOUNT, top=100, extra=""):
        return f"<input id=i value='{value}' style='{self.BOX}top:{top}px;' {extra}>"

    # -- the value is never written --------------------------------------------------------------

    def test_the_overlay_shows_the_masked_value_over_the_input_and_the_value_stays_raw(self):
        self.open(self.page_with(self.input_html()), spy=True)
        self.wait_overlays(1)
        ov = self.one()
        self.assertEqual(ov["text"], "********1234")
        self.assertNotIn(ACCOUNT, ov["text"])
        self.assertCovers(ov, self.rect("#i"))
        self.assertEqual(self.ev("() => document.getElementById('i').value"), ACCOUNT)
        # blur, input/change events and several more intervals: still raw, still overlaid, nothing written
        self.ev("() => { const i = document.getElementById('i'); i.focus(); i.blur();"
                " i.dispatchEvent(new Event('input', {bubbles: true})); i.dispatchEvent(new Event('change', {bubbles: true})); }")
        time.sleep(0.2)
        self.assertEqual(self.ev("() => document.getElementById('i').value"), ACCOUNT)
        self.assertEqual(self.ev("() => window.__valueWrites"), 0, "the overlay must never assign el.value")
        self.assertEqual(len(self.overlays()), 1)
        self.assertEqual(self.errors, [])

    def test_a_textarea_gets_an_overlay_too_and_keeps_its_raw_value(self):
        area = (f"<textarea id=t style='{self.BOX}top:100px;height:60px;'>note {ACCOUNT} end</textarea>")
        self.open(self.page_with(area), spy=True)
        self.wait_overlays(1)
        ov = self.one()
        self.assertEqual(ov["text"], "note ********1234 end")
        self.assertCovers(ov, self.rect("#t"))
        self.assertEqual(self.ev("() => document.getElementById('t').value"), f"note {ACCOUNT} end")
        self.assertEqual(self.ev("() => window.__valueWrites"), 0)

    def test_every_match_in_one_value_is_masked(self):
        self.open(self.page_with(self.input_html("5500123412341234 / 5500999988887777")))
        self.wait_overlays(1)
        self.assertEqual(self.one()["text"], "********1234 / ********7777")

    def test_a_non_matching_input_gets_no_overlay(self):
        self.open(self.page_with(self.input_html("hello 55001234") + "<input id=e style='position:absolute;top:10px'>"))
        time.sleep(0.25)                     # many 20 ms intervals
        self.assertEqual(self.overlays(), [])

    def test_quotes_backslashes_and_dollars_in_pattern_and_replacement_are_safe(self):
        rep = "it's \"x\" \\ end"
        self.open(self.page_with(self.input_html('say "secret" now')), pattern=r'"secret"', replacement=rep)
        self.wait_overlays(1)
        self.assertEqual(self.one()["text"], f"say {rep} now")
        self.assertEqual(self.errors, [])

    def test_no_script_error_while_the_document_has_no_body_yet(self):
        self.open(f"<html><head><script src='/slow.js'></script></head><body>{self.input_html()}</body></html>", slow_script=0.4)
        self.wait_overlays(1)
        self.assertEqual(self.errors, [], "a sync tick before <body> exists threw")

    # -- the overlay must hide the raw text and never get in the way ------------------------------

    def test_overlay_is_fixed_on_top_and_lets_the_mouse_through(self):
        self.open(self.page_with(self.input_html()))
        self.wait_overlays(1)
        ov = self.one()
        self.assertEqual(ov["pos"], "fixed")
        self.assertEqual(ov["pe"], "none")
        self.assertEqual(ov["z"], "2147483647", "must sit above a modal (z-index 1055)")
        r = self.rect("#i")
        cx, cy = r["left"] + r["width"] / 2, r["top"] + r["height"] / 2
        self.assertEqual(self.ev("([x, y]) => document.elementFromPoint(x, y).id", [cx, cy]), "i",
                         "a click on the input must reach the input")
        self.run_(self.page.mouse.click(cx, cy))
        self.assertEqual(self.ev("() => document.activeElement.id"), "i")

    def test_overlay_background_hides_the_raw_text_even_for_a_transparent_input(self):
        html = self.page_with("<div style='position:absolute;left:0;top:0;width:600px;height:300px;background:rgb(255,0,0)'>"
                              f"<input id=i value='{ACCOUNT}' style='{self.BOX}top:100px;background:transparent;border:0;'></div>")
        self.open(html)
        self.wait_overlays(1)
        self.assertEqual(self.one()["bg"], "rgb(255, 0, 0)", "the overlay takes the first opaque ancestor background")

    def test_overlay_of_an_ordinary_input_has_an_opaque_background(self):
        self.open(self.page_with(self.input_html()))
        self.wait_overlays(1)
        self.assertNotIn(self.one()["bg"], ("rgba(0, 0, 0, 0)", "transparent"))

    def test_a_hidden_input_gets_no_overlay_until_it_is_shown(self):
        hidden = f"<input id=h value='{ACCOUNT}' style='display:none'>"
        self.open(self.page_with(self.input_html() + hidden))
        self.wait_overlays(1)                # the visible one; the display:none one has a 0x0 rect
        time.sleep(0.15)
        self.assertEqual(len(self.overlays()), 1)
        self.ev("() => { const h = document.getElementById('h'); h.style.cssText = 'position:absolute;left:300px;top:200px;width:100px;height:20px'; }")
        self.wait_overlays(2)

    def test_a_custom_selector_limits_which_inputs_are_masked(self):
        both = self.input_html() + f"<input id=only value='{ACCOUNT}' style='{self.BOX}top:200px;'>"
        self.open(self.page_with(both), selector="#only")
        self.wait_overlays(1)
        time.sleep(0.15)
        self.assertEqual(len(self.overlays()), 1)
        self.assertCovers(self.one(), self.rect("#only"))

    # -- the overlay follows the input and goes away with it -------------------------------------

    def test_overlay_follows_the_input_after_a_window_scroll(self):
        tall = self.input_html() + "<div style='position:absolute;top:0;left:0;width:10px;height:3000px'></div>"
        self.open(self.page_with(tall))
        self.wait_overlays(1)
        self.ev("() => window.scrollTo(0, 70)")
        self.wait_covering("#i")
        self.assertEqual(self.ev("() => window.scrollY"), 70)
        self.assertCovers(self.one(), self.rect("#i"))
        self.assertAlmostEqual(self.one()["top"], 30, delta=1.0, msg="input top 100 - scroll 70")

    def test_the_scroll_listener_alone_repositions_the_overlay_with_no_interval(self):
        # interval ~2.8 h: only addEventListener('scroll', sync) can create / move the overlay here
        tall = self.input_html() + "<div style='position:absolute;top:0;left:0;width:10px;height:3000px'></div>"
        self.open(self.page_with(tall), interval_ms=self.HUGE)
        self.ev("() => window.scrollTo(0, 40)")
        self.wait_overlays(1)
        self.wait_covering("#i")
        self.ev("() => window.scrollTo(0, 90)")
        self.wait_covering("#i")
        self.assertAlmostEqual(self.one()["top"], 10, delta=1.0)

    def test_an_inner_scroll_container_scroll_moves_the_overlay_too(self):
        # element scroll events do not bubble: the listener must be a CAPTURING one on window
        inner = (f"<div id=sc style='position:absolute;left:0;top:50px;width:500px;height:200px;overflow:auto'>"
                 f"<div style='height:20px'></div><input id=i value='{ACCOUNT}' style='{self.BOX}position:relative;left:0;'>"
                 "<div style='height:900px'></div></div>")
        self.open(self.page_with(inner), interval_ms=self.HUGE)
        self.run_(self.page.set_viewport_size({"width": 1100, "height": 760}))     # resize -> first sync creates the overlay
        self.wait_overlays(1)
        self.wait_covering("#i")
        self.ev("() => { document.getElementById('sc').scrollTop = 15; }")
        self.wait_covering("#i")
        self.assertEqual(self.ev("() => document.getElementById('sc').scrollTop"), 15)

    def test_the_resize_listener_alone_creates_and_resizes_the_overlay_with_no_interval(self):
        wide = f"<input id=i value='{ACCOUNT}' style='position:absolute;left:10px;top:100px;width:30vw;height:30px;box-sizing:border-box'>"
        self.open(self.page_with(wide), interval_ms=self.HUGE)
        self.run_(self.page.set_viewport_size({"width": 1000, "height": 700}))
        self.wait_overlays(1)
        self.wait_fn("() => Math.abs(document.querySelector('[data-demo-mask]').getBoundingClientRect().width - 300) < 1",
                     what="overlay width 300 (30vw of 1000)")
        self.run_(self.page.set_viewport_size({"width": 600, "height": 700}))
        self.wait_fn("() => Math.abs(document.querySelector('[data-demo-mask]').getBoundingClientRect().width - 180) < 1",
                     what="overlay width 180 (30vw of 600)")

    def test_overlay_disappears_when_the_value_no_longer_matches_and_comes_back_and_updates(self):
        two = (self.input_html(top=100)
               + f"<input id=j value='5500999988887777' style='{self.BOX}top:200px;'>")
        self.open(self.page_with(two))
        self.wait_overlays(2)
        self.ev("() => { document.getElementById('i').value = 'plain text'; }")
        self.wait_overlays(1)
        self.assertEqual(self.one()["text"], "********7777", "the OTHER input keeps its overlay")
        self.assertEqual(self.ev("() => document.getElementById('i').value"), "plain text")
        self.ev("() => { document.getElementById('j').value = '5500123412341234'; }")
        self.wait_fn("() => document.querySelector('[data-demo-mask]').textContent === '********1234'",
                     what="overlay text to follow the new value")
        self.ev("() => { document.getElementById('i').value = '5500555566667777'; }")
        self.wait_overlays(2)

    def test_overlay_disappears_when_the_input_leaves_the_page(self):
        two = (f"<div id=box>{self.input_html(top=100)}</div>"
               f"<input id=j value='5500999988887777' style='{self.BOX}top:200px;'>")
        self.open(self.page_with(two))
        self.wait_overlays(2)
        self.ev("() => document.getElementById('box').remove()")        # the input goes with its parent
        self.wait_overlays(1)
        self.assertEqual(self.one()["text"], "********7777")
        self.ev("() => document.getElementById('j').remove()")
        self.wait_overlays(0)

    def test_an_overlay_is_not_masked_text_of_its_own_and_survives_many_ticks_without_duplicating(self):
        self.open(self.page_with(self.input_html()))
        self.wait_overlays(1)
        time.sleep(0.3)
        self.assertEqual(len(self.overlays()), 1, "one overlay per input, reused on every tick")


class TestAssertFormCleanInBrowser(RealChromeCase):
    """th.assert_form_clean against the markup of Odoo 17 form_status_indicator.xml:
    <div class="o_form_status_indicator_buttons d-flex" t-att-class="{invisible: !(isNew or displayButtons)}">."""

    def form(self, cls):
        self.serve(f"<html><body><div class=o_form_view><div class='o_form_status_indicator'>"
                   f"<div class='{cls}'><button>Save</button><button>Discard</button></div></div></div></body></html>")

    def test_an_invisible_indicator_means_a_clean_form(self):
        self.form("o_form_status_indicator_buttons d-flex invisible")
        self.assertIsNone(self.run_(th.assert_form_clean(self.page)))

    def test_a_shown_indicator_means_unsaved_changes(self):
        self.form("o_form_status_indicator_buttons d-flex")
        with self.assertRaises(RuntimeError) as cm:
            self.run_(th.assert_form_clean(self.page))
        self.assertIn("unsaved changes", str(cm.exception))

    def test_the_form_turning_dirty_after_the_page_loaded_is_seen(self):
        self.form("o_form_status_indicator_buttons d-flex invisible")
        self.run_(th.assert_form_clean(self.page))
        self.ev("() => document.querySelector('.o_form_status_indicator_buttons').classList.remove('invisible')")
        with self.assertRaises(RuntimeError):
            self.run_(th.assert_form_clean(self.page))

    def test_no_indicator_at_all_is_clean_and_a_custom_selector_is_honoured(self):
        self.serve("<html><body><div class=o_form_view></div></body></html>")
        self.assertIsNone(self.run_(th.assert_form_clean(self.page)))
        self.form("o_form_status_indicator_buttons d-flex invisible")
        self.assertIsNone(self.run_(th.assert_form_clean(self.page)))
        with self.assertRaises(RuntimeError):
            self.run_(th.assert_form_clean(self.page, selector=".o_form_status_indicator_buttons"))


class TestLabelMatchInBrowser(RealChromeCase):
    """exact_text_re against REAL Playwright locators (the twin tests only see the pattern)."""

    NAVBAR = ("<html><body><div class=o_main_navbar>"
              "<a id=acc>Account</a><a id=accing>Accounting</a><a id=rep>Account Reports</a>"
              "<a id=dash style='text-transform: uppercase'>Test call</a></div>"
              "<div class=dropdown-menu style='display:block'><a id=i1>Transactions</a><a id=i2>transactions old</a>"
              "<a id=i3 style='text-transform: uppercase'>Acme bank</a></div>"
              "<script>window.__clicks=[]; addEventListener('click', e => __clicks.push(e.target.id), {capture:true});</script>"
              "</body></html>")

    def count(self, locator):
        return self.run_(locator.count())

    def test_get_by_text_regex_matches_a_label_whose_dom_case_differs(self):
        self.serve(self.NAVBAR)
        bar = self.page.locator(".o_main_navbar")
        self.assertEqual(self.count(bar.get_by_text(ch.exact_text_re("Test Call"))), 1, "DOM 'Test call', shown 'Test Call'")
        self.assertEqual(self.count(bar.get_by_text("Test Call", exact=True)), 0,
                         "premise: the old exact=True string match is case-sensitive and would NOT find it")

    def test_get_by_text_regex_is_a_whole_label_match(self):
        self.serve(self.NAVBAR)
        bar = self.page.locator(".o_main_navbar")
        found = self.run_(bar.get_by_text(ch.exact_text_re("Account")).evaluate_all("(els) => els.map(e => e.id)"))
        self.assertEqual(found, ["acc"], "'Account' must not also match Accounting / Account Reports")

    def test_filter_has_text_regex_matches_case_insensitively(self):
        self.serve("<html><body><div class=o_form_view><button>Test call</button><button>Test call all</button></div></body></html>")
        btns = self.page.locator(".o_form_view button:visible").filter(has_text=ch.exact_text_re("Test Call"))
        self.assertEqual(self.count(btns), 1)

    def test_navbar_and_dropdown_item_click_only_the_whole_label_match(self):
        self.serve(self.NAVBAR)
        cur = ch.CursorState(10, 10)
        self.run_(ch.navbar(self.page, cur, "account", pause=0, settle=0))
        self.run_(ch.dropdown_item(self.page, cur, "TRANSACTIONS", pause=0, settle=0))
        self.run_(ch.dropdown_item(self.page, cur, "acme BANK", pause=0, settle=0))
        self.run_(ch.navbar(self.page, cur, "Test Call", pause=0, settle=0))
        self.assertEqual(self.run_(self.page.evaluate("() => __clicks")), ["acc", "i1", "i3", "dash"])


class TestPressButtonInBrowser(RealChromeCase):
    # `#go` raises a notification 300 ms after the click; `#other` shares a prefix; `#old` is a toast
    # that is on screen at the start and leaves at OLD_GONE_MS.
    PAGE = """<html><body>
      <div class=o_form_view>
        <button id=other>Test call all</button>
        <button id=go style='text-transform: uppercase'>Test call</button>
      </div>
      <div id=toasts></div>
      <script>
        window.__log = [];
        const t0 = performance.now();
        const toasts = document.getElementById('toasts');
        const toast = (txt) => { const d = document.createElement('div'); d.className = 'o_notification'; d.textContent = txt; toasts.appendChild(d); return d; };
        addEventListener('click', e => __log.push(['click', e.target.id, Math.round(performance.now() - t0)]), {capture: true});
        document.getElementById('go').addEventListener('click', () => {
          if (window.__raise !== false) setTimeout(() => toast('new'), 300);
          if (window.__dialogs) setTimeout(() => window.__dialogs.forEach((t) => {
            const d = document.createElement('div'); d.className = 'modal-content o_error_dialog'; d.textContent = t;
            document.body.appendChild(d); }), 300);
        });
        %s
      </script></body></html>"""
    OLD_GONE_MS = 900

    def open(self, script: str = ""):
        self.serve(self.PAGE % script)
        return ch.CursorState(5, 5)

    def log(self):
        return self.run_(self.page.evaluate("() => __log"))

    def test_waits_for_the_old_toast_to_leave_then_clicks_the_right_button_then_sees_the_new_toast(self):
        cur = self.open(f"toast('old'); setTimeout(() => document.querySelector('.o_notification').remove(), {self.OLD_GONE_MS});")
        t0 = time.monotonic()
        self.run_(ch.press_button(self.page, cur, "Test Call", pause=0.05, settle=0.05, toast_timeout=5, prev_timeout=5))
        elapsed = time.monotonic() - t0
        clicks = self.log()
        self.assertEqual([c[1] for c in clicks], ["go"], "only the whole-label, case-insensitive match is clicked")
        self.assertGreaterEqual(clicks[0][2], self.OLD_GONE_MS - 100, f"clicked at {clicks[0][2]} ms, before the old toast left")
        self.assertGreaterEqual(elapsed, (self.OLD_GONE_MS + 300) / 1000, "returned before the NEW toast could appear")
        self.assertEqual(self.run_(self.page.locator(".o_notification").count()), 1)
        self.assertEqual(self.text(".o_notification"), "new")

    def test_the_settle_hold_comes_after_the_new_toast(self):
        cur = self.open()
        t0 = time.monotonic()
        self.run_(ch.press_button(self.page, cur, "Test Call", pause=0, settle=0.8, toast_timeout=5))
        self.assertGreaterEqual(time.monotonic() - t0, 0.3 + 0.8, "settle must be held AFTER the toast appeared")

    def test_no_new_toast_raises_a_playwright_timeout(self):
        cur = self.open("window.__raise = false;")
        with self.assertRaises(Exception) as cm:
            self.run_(ch.press_button(self.page, cur, "Test Call", pause=0, settle=0, toast_timeout=0.6))
        self.assertEqual(type(cm.exception).__name__, "TimeoutError")
        self.assertEqual([c[1] for c in self.log()], ["go"], "the click happened, the toast never came")

    def test_a_stuck_old_toast_is_reported_and_the_click_still_happens(self):
        cur = self.open("toast('stuck'); window.__raise = false;")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            # no raise: the stuck toast is itself a visible .o_notification, so the "new toast" wait passes at once
            self.run_(ch.press_button(self.page, cur, "Test Call", pause=0, settle=0, toast_timeout=2, prev_timeout=0.4))
        self.assertIn("previous toast still shown", err.getvalue())
        self.assertEqual([c[1] for c in self.log()], ["go"])

    # --- an Odoo error dialog (Validation Error / Server Error) shows NO toast: raise at once ---

    def test_an_error_dialog_instead_of_a_toast_raises_at_once_with_its_text(self):
        cur = self.open("window.__raise = false; window.__dialogs = ['Validation Error: Bank account is required'];")
        t0 = time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            self.run_(ch.press_button(self.page, cur, "Test Call", pause=0, settle=3.0, toast_timeout=5))
        self.assertLess(time.monotonic() - t0, 2.0, "an error dialog must not wait for the toast timeout or the settle hold")
        self.assertIn("press_button('Test Call')", str(cm.exception))
        self.assertIn("Bank account is required", str(cm.exception))
        self.assertEqual([c[1] for c in self.log()], ["go"], "the click happened, only the failure is reported")

    def test_stacked_error_dialogs_raise_runtime_error_not_a_strict_mode_error(self):
        cur = self.open("window.__raise = false; window.__dialogs = ['first dialog', 'second dialog'];")
        with self.assertRaises(RuntimeError) as cm:
            self.run_(ch.press_button(self.page, cur, "Test Call", pause=0, settle=0, toast_timeout=5))
        self.assertIn("first dialog", str(cm.exception))
        self.assertNotIn("second dialog", str(cm.exception))

    def test_a_long_error_dialog_text_is_cut_to_300_chars(self):
        cur = self.open("window.__raise = false; window.__dialogs = ['  ' + 'y'.repeat(500) + '  '];")
        with self.assertRaises(RuntimeError) as cm:
            self.run_(ch.press_button(self.page, cur, "Test Call", pause=0, settle=0, toast_timeout=5))
        self.assertIn(repr("y" * 300), str(cm.exception))
        self.assertNotIn("y" * 301, str(cm.exception))

    def test_a_toast_without_a_dialog_still_returns_normally(self):
        cur = self.open()          # the page raises 'new' after 300 ms and no dialog
        self.run_(ch.press_button(self.page, cur, "Test Call", pause=0, settle=0, toast_timeout=5))
        self.assertEqual(self.run_(self.page.locator(".o_error_dialog").count()), 0)

    def test_wait_toast_false_clicks_and_returns_at_once(self):
        cur = self.open("toast('old');")                 # an old toast stays: wait_toast=False must not wait for it
        t0 = time.monotonic()
        self.run_(ch.press_button(self.page, cur, "Test Call", wait_toast=False, pause=0))
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual([c[1] for c in self.log()], ["go"])

    def test_scope_limits_the_search(self):
        self.serve("<html><body><div class=o_form_view><button id=in_form>Save</button></div>"
                   "<div class=o_list_view><button id=in_list>Save</button></div>"
                   "<script>window.__log=[]; addEventListener('click', e => __log.push(['click', e.target.id]), {capture:true});</script></body></html>")
        cur = ch.CursorState(5, 5)
        self.run_(ch.press_button(self.page, cur, "Save", scope=".o_list_view", wait_toast=False, pause=0))
        self.assertEqual([c[1] for c in self.log()], ["in_list"])


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
        except Exception as e:  # noqa: BLE001 — any launch failure -> try the next binary
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
