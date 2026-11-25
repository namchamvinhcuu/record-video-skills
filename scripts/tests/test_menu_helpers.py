#!/usr/bin/env python3
"""Guided-tour menu helpers of click_helpers.py (sidebar / navbar / dropdown_item / menu_path /
refresh_list) — no browser. A recording page twin logs, in ONE ordered list, every locator chain
handed to `click_box`, every sleep and every key press, so the tests assert order + selectors.

The twin page has NO `goto` / `reload`: a helper that navigated by URL would raise AttributeError
here — the guided-tour rule (never page.goto inside the visible part of a video) is enforced by
construction.

Run: python3 shared/skills/record-demo-video/scripts/tests/test_menu_helpers.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import re
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("click_helpers_menu_mod", HERE.parent / "click_helpers.py")
assert _spec and _spec.loader
ch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ch)


class Chain:
    """A locator chain: records the calls that built it (locator / filter / get_by_text / first).
    `cfg` is shared by the whole chain tree of one Page: {"count": N, "text": str} = what an
    awaited `.count()` / `.inner_text()` answers (the error-dialog probe of press_button)."""

    def __init__(self, log, steps=(), cfg=None):
        self.log, self.steps = log, tuple(steps)
        self.cfg = cfg if cfg is not None else {"count": 0, "text": ""}

    def _next(self, step):
        return Chain(self.log, self.steps + (step,), self.cfg)

    def locator(self, selector):
        return self._next(("locator", selector))

    def filter(self, has_text=None):
        return self._next(("filter", has_text))

    def get_by_text(self, text, exact=False):
        return self._next(("get_by_text", text, exact))

    @property
    def first(self):
        return self._next(("first",))

    async def count(self):
        self.log.append(("count", self.steps))
        return self.cfg["count"]

    async def inner_text(self):
        self.log.append(("inner_text", self.steps))
        return self.cfg["text"]


class Keyboard:
    def __init__(self, log):
        self.log = log

    async def press(self, key):
        self.log.append(("press", key))


class Page(Chain):
    def __init__(self, log, wait_fn_error=None, wait_sel_error=None, dialog_count=0, dialog_text=""):
        super().__init__(log, cfg={"count": dialog_count, "text": dialog_text})
        self.keyboard = Keyboard(log)
        self.wait_fn_error, self.wait_sel_error = wait_fn_error, wait_sel_error

    async def wait_for_function(self, expression, timeout=None):
        self.log.append(("wait_fn", expression, timeout))
        if self.wait_fn_error:
            raise self.wait_fn_error

    async def wait_for_selector(self, selector, state=None, timeout=None):
        self.log.append(("wait_sel", selector, state, timeout))
        if self.wait_sel_error:
            raise self.wait_sel_error


def run_calls(coro_fn, *args, fail_on_click=None, wait_fn_error=None, wait_sel_error=None,
              dialog_count=0, dialog_text="", **kw):
    """Run coro_fn(page, cursor, *args) with click_box and asyncio.sleep faked.
    Returns (log, calls, page, cursor, error): `log` = ordered click/sleep/press events; `calls` = one
    (page, cursor, kwargs) per click_box call, to check WHAT was handed over, not only the order;
    `error` = the exception the helper raised (None when it finished).
    fail_on_click=N -> the Nth click_box call (1-based) raises RuntimeError("boom") after logging.
    dialog_count / dialog_text = what `.count()` / `.inner_text()` answer (an error dialog on screen)."""
    log: list = []
    calls: list = []
    page, cursor = Page(log, wait_fn_error, wait_sel_error, dialog_count, dialog_text), ch.CursorState()

    async def fake_click_box(pg, locator, cur, **extra):
        calls.append((pg, cur, extra))
        log.append(("click", locator.steps))
        if fail_on_click == len(calls):
            raise RuntimeError("boom")

    async def fake_sleep(seconds, *a, **k):
        log.append(("sleep", seconds))

    async def main():
        await coro_fn(page, cursor, *args, **kw)

    error = None
    with mock.patch.object(ch, "click_box", fake_click_box), mock.patch.object(ch.asyncio, "sleep", fake_sleep):
        try:
            asyncio.run(main())
        except Exception as exc:  # returned, so a test can assert on it AND on the log
            error = exc
    return log, calls, page, cursor, error


def run(coro_fn, *args, **kw):
    """Run coro_fn(page, cursor, *args) with click_box and asyncio.sleep faked; return the log.
    An error raised by the helper fails the test (use run_calls to inspect an expected error)."""
    log, _, _, _, error = run_calls(coro_fn, *args, **kw)
    if error is not None:
        raise error
    return log


def clicks(log):
    return [e[1] for e in log if e[0] == "click"]


def selector_of(steps):
    return next(s[1] for s in steps if s[0] == "locator")


def name_of(steps):
    """The regex pattern a locator chain looks for: the filter regex (sidebar, press_button) or the
    get_by_text regex (navbar, dropdown_item)."""
    for s in steps:
        if s[0] == "get_by_text":
            return s[1].pattern
    return next(s[1].pattern for s in steps if s[0] == "filter")


class TestExactTextRe(unittest.TestCase):
    def test_matches_the_whole_label_only(self):
        r = ch.exact_text_re("Account")
        self.assertTrue(r.search("Account"))
        self.assertTrue(r.search("  Account \n"), "surrounding whitespace is allowed")
        self.assertFalse(r.search("Accounting"))
        self.assertFalse(r.search("Account Reports"))
        self.assertFalse(r.search("My Account"))

    def test_case_insensitive_for_css_text_transform(self):
        r = ch.exact_text_re("Test Call")
        self.assertTrue(r.search("Test call"), "DOM text 'Test call' is shown as 'Test Call' by the theme")
        self.assertTrue(r.search("TEST CALL"))
        self.assertFalse(r.search("Test calls"), "case-insensitive must still be a whole-label match")

    def test_case_folding_covers_non_ascii_labels(self):
        # Vietnamese / accented labels: CSS text-transform uppercases them too, and an ASCII-only
        # fold (re.ASCII) would silently stop matching "đã gửi" vs "ĐÃ GỬI".
        r = ch.exact_text_re("Đã gửi")
        self.assertTrue(r.search("ĐÃ GỬI"))
        self.assertTrue(r.search("đã gửi"))
        self.assertFalse(r.search("Đã gửi lại"))

    def test_a_multiline_text_is_not_a_whole_label(self):
        # An element whose text spans several lines must not match on one of its lines
        # (re.MULTILINE would turn ^/$ into line anchors and click "Account\nReports" as "Account").
        r = ch.exact_text_re("Account")
        self.assertFalse(r.search("Account\nReports"))
        self.assertFalse(r.search("Reports\nAccount"))
        self.assertFalse(r.search("Reports\nAccount\nSettings"))

    def test_special_characters_are_escaped(self):
        self.assertTrue(ch.exact_text_re("Accounting & Tax").search("Accounting & Tax"))
        self.assertTrue(ch.exact_text_re("A+B (x)").search("A+B (x)"))
        self.assertFalse(ch.exact_text_re("A+B").search("AAB"), "'+' must be literal, not a quantifier")


class TestSidebar(unittest.TestCase):
    def test_pause_then_click_then_settle(self):
        log = run(ch.sidebar, "Configuration")
        self.assertEqual([e[0] for e in log], ["sleep", "click", "sleep"])
        self.assertEqual(log[0], ("sleep", ch.MENU_PAUSE))
        self.assertEqual(log[2], ("sleep", ch.SIDEBAR_SETTLE))

    def test_targets_the_visible_link_with_the_exact_label(self):
        (steps,) = clicks(run(ch.sidebar, "Configuration"))
        self.assertEqual(selector_of(steps), "a:visible")
        flt = next(s for s in steps if s[0] == "filter")
        self.assertEqual(flt[1].pattern, ch.exact_text_re("Configuration").pattern)
        self.assertEqual(steps[-1], ("first",))

    def test_custom_pause_and_settle_are_honoured(self):
        log = run(ch.sidebar, "Account", pause=0.1, settle=0.2)
        self.assertEqual([e for e in log if e[0] == "sleep"], [("sleep", 0.1), ("sleep", 0.2)])


class TestClickHandover(unittest.TestCase):
    """Every helper must hand click_box the SAME page + cursor and NO extra kwargs: a stray
    scroll=False would skip the scroll + hit-test, a fresh CursorState would teleport the mouse."""

    CASES = [("sidebar", ch.sidebar, ("Configuration",)), ("navbar", ch.navbar, ("Accounting & Tax",)),
             ("dropdown_item", ch.dropdown_item, ("Transactions",)), ("refresh_list", ch.refresh_list, ())]

    def test_page_cursor_and_default_kwargs_reach_click_box(self):
        for label, fn, args in self.CASES:
            _, calls, page, cursor, error = run_calls(fn, *args)
            self.assertIsNone(error, label)
            self.assertEqual(len(calls), 1, label)
            self.assertIs(calls[0][0], page, label)
            self.assertIs(calls[0][1], cursor, label)
            self.assertEqual(calls[0][2], {}, f"{label}: click_box must run with its defaults (scroll + hit-test)")

    def test_menu_path_hands_every_click_the_same_page_and_cursor(self):
        _, calls, page, cursor, error = run_calls(ch.menu_path, ["Configuration", "Accounting & Tax", "Transactions"])
        self.assertIsNone(error)
        self.assertEqual(len(calls), 3)
        for pg, cur, extra in calls:
            self.assertIs(pg, page)
            self.assertIs(cur, cursor)
            self.assertEqual(extra, {})

    def test_pause_and_settle_defaults_are_real_waits(self):
        # a 0 pause = the mouse teleports onto the target (the thing this module exists to prevent);
        # a 0 settle = the next click lands before the screen/dropdown has rendered.
        # (TOAST_SETTLE: press_button's default hold; a 0 leaves no time for the toast to be read.)
        for const in (ch.MENU_PAUSE, ch.SIDEBAR_SETTLE, ch.NAVBAR_SETTLE, ch.TOAST_SETTLE):
            self.assertGreater(const, 0)


class TestNavbar(unittest.TestCase):
    def test_pause_then_click_then_settle(self):
        log = run(ch.navbar, "Accounting & Tax")
        self.assertEqual(log[0], ("sleep", ch.MENU_PAUSE))
        self.assertEqual(log[1][0], "click")
        self.assertEqual(log[2], ("sleep", ch.NAVBAR_SETTLE))

    def test_targets_the_exact_text_inside_the_main_navbar(self):
        (steps,) = clicks(run(ch.navbar, "Accounting & Tax"))
        self.assertEqual(selector_of(steps), ".o_main_navbar")
        self.assertEqual(name_of(steps), ch.exact_text_re("Accounting & Tax").pattern)
        gbt = next(s for s in steps if s[0] == "get_by_text")
        self.assertTrue(gbt[1].flags & re.IGNORECASE, "a CSS text-transform must not break the match")
        self.assertEqual(steps[-1], ("first",))

    def test_custom_pause_and_settle_are_honoured(self):
        log = run(ch.navbar, "Dashboard", pause=0.1, settle=0.2)
        self.assertEqual([e for e in log if e[0] == "sleep"], [("sleep", 0.1), ("sleep", 0.2)])


class TestDropdownItem(unittest.TestCase):
    def test_pause_then_click_and_no_trailing_sleep_by_default(self):
        log = run(ch.dropdown_item, "Acme Bank accounts")
        self.assertEqual([e[0] for e in log], ["sleep", "click"])
        self.assertEqual(log[0], ("sleep", ch.MENU_PAUSE))

    def test_targets_the_exact_text_in_the_open_dropdown(self):
        (steps,) = clicks(run(ch.dropdown_item, "Acme Bank accounts"))
        self.assertEqual(selector_of(steps), ".dropdown-menu:visible")
        self.assertEqual(name_of(steps), ch.exact_text_re("Acme Bank accounts").pattern)
        gbt = next(s for s in steps if s[0] == "get_by_text")
        self.assertTrue(gbt[1].flags & re.IGNORECASE, "a CSS text-transform must not break the match")
        self.assertEqual(steps[-1], ("first",), "several items may share a label (nested menus): take the first")

    def test_custom_pause_is_honoured(self):
        self.assertEqual(run(ch.dropdown_item, "Transactions", pause=0.3)[0], ("sleep", 0.3))

    def test_settle_adds_one_trailing_sleep(self):
        log = run(ch.dropdown_item, "Transactions", settle=2.0)
        self.assertEqual([e[0] for e in log], ["sleep", "click", "sleep"])
        self.assertEqual(log[2], ("sleep", 2.0))


class TestMenuPath(unittest.TestCase):
    def selectors(self, log):
        return [selector_of(c) for c in clicks(log)]

    def test_app_navbar_dropdown_is_three_real_clicks_in_order(self):
        log = run(ch.menu_path, ["Configuration", "Accounting & Tax", "Acme Bank accounts"])
        self.assertEqual(self.selectors(log), ["a:visible", ".o_main_navbar", ".dropdown-menu:visible"])

    def test_app_and_plain_navbar_entry(self):
        log = run(ch.menu_path, ["Account", "Dashboard"])
        self.assertEqual(self.selectors(log), ["a:visible", ".o_main_navbar"])

    def test_without_app_starts_at_the_navbar(self):
        log = run(ch.menu_path, ["Accounting & Tax", "Acme Bank transactions"], app=False)
        self.assertEqual(self.selectors(log), [".o_main_navbar", ".dropdown-menu:visible"])

    def test_without_app_single_navbar_entry(self):
        log = run(ch.menu_path, ["Dashboard"], app=False)
        self.assertEqual(self.selectors(log), [".o_main_navbar"])

    def test_every_click_is_preceded_by_the_viewer_pause(self):
        log = run(ch.menu_path, ["Configuration", "Accounting & Tax", "Acme Bank accounts"])
        for i, event in enumerate(log):
            if event[0] == "click":
                self.assertEqual(log[i - 1], ("sleep", ch.MENU_PAUSE), "the mouse must be seen arriving")

    def test_each_level_receives_its_own_label(self):
        # three DIFFERENT names: a helper handed the wrong list item clicks the wrong menu
        log = run(ch.menu_path, ["App X", "Navbar Y", "Item Z"])
        sidebar_c, navbar_c, dropdown_c = clicks(log)
        self.assertEqual(name_of(sidebar_c), ch.exact_text_re("App X").pattern)
        self.assertEqual(name_of(navbar_c), ch.exact_text_re("Navbar Y").pattern)
        self.assertEqual(name_of(dropdown_c), ch.exact_text_re("Item Z").pattern)

    def test_without_app_labels_are_not_shifted(self):
        log = run(ch.menu_path, ["Navbar Y", "Item Z"], app=False)
        navbar_c, dropdown_c = clicks(log)
        self.assertEqual(name_of(navbar_c), ch.exact_text_re("Navbar Y").pattern)
        self.assertEqual(name_of(dropdown_c), ch.exact_text_re("Item Z").pattern)

    def test_the_callers_path_list_is_not_modified(self):
        path = ["Configuration", "Accounting & Tax", "Transactions"]
        run(ch.menu_path, path)
        self.assertEqual(path, ["Configuration", "Accounting & Tax", "Transactions"])

    def test_a_failing_click_stops_the_walk_and_propagates(self):
        # Regression guard: an error mid-path (menu missing, hit-test fails) must surface, and the
        # following levels must NOT be clicked blind on a screen the previous click never reached.
        path = ["Configuration", "Accounting & Tax", "Transactions"]
        for failing in (1, 2, 3):
            log, calls, _, _, error = run_calls(ch.menu_path, path, fail_on_click=failing)
            self.assertIsInstance(error, RuntimeError, f"click #{failing} must surface its error")
            self.assertEqual(len(calls), failing, f"clicks attempted after click #{failing} failed")

    def test_empty_path_with_app_says_so(self):
        with mock.patch.object(ch, "click_box", side_effect=AssertionError("clicked")):
            with self.assertRaisesRegex(ValueError, "empty path"):
                asyncio.run(ch.menu_path(Page([]), ch.CursorState(), [], app=True))

    def test_bad_paths_raise_before_any_click(self):
        bad = [([], True), (["Configuration"], True), (["A", "B", "C", "D"], True),
               (["A", "B", "C"], False), ([], False)]
        for path, app in bad:
            log: list = []

            async def go():
                await ch.menu_path(Page(log), ch.CursorState(), path, app=app)

            with mock.patch.object(ch, "click_box", side_effect=AssertionError("clicked")):
                with self.assertRaises(ValueError, msg=repr((path, app))):
                    asyncio.run(go())
            self.assertEqual(log, [], "nothing may happen on a bad path")


class TestRefreshList(unittest.TestCase):
    def test_click_search_box_then_enter_then_settle(self):
        log = run(ch.refresh_list)
        self.assertEqual([e[0] for e in log], ["click", "press", "sleep"])
        self.assertEqual(selector_of(log[0][1]), ".o_searchview_input")
        self.assertEqual(log[1], ("press", "Enter"))
        self.assertEqual(log[2], ("sleep", 1.5))

    def test_custom_settle(self):
        self.assertEqual(run(ch.refresh_list, settle=0.5)[-1], ("sleep", 0.5))

    def test_a_failing_click_means_no_enter_and_the_error_propagates(self):
        # Regression guard: Enter pressed with focus elsewhere (the search box click failed) would
        # submit whatever else is focused (a form field, a dialog button).
        log, calls, _, _, error = run_calls(ch.refresh_list, fail_on_click=1)
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIn(("press", "Enter"), log)
        self.assertEqual([e for e in log if e[0] == "sleep"], [], "no settle wait after a failed refresh")


class TestPressButton(unittest.TestCase):
    def kinds(self, log):
        return [e[0] for e in log]

    def test_toast_button_waits_old_toast_clicks_waits_new_toast_then_holds(self):
        log = run(ch.press_button, "Test Call")
        self.assertEqual(self.kinds(log), ["wait_fn", "sleep", "click", "wait_sel", "count", "sleep"])
        # "no toast in the DOM" — inverting it (or aiming at another selector) waits for the wrong thing
        self.assertEqual(log[0][1], "() => !document.querySelector('.o_notification')")
        self.assertEqual(log[0][2], 15000.0, "previous toast wait: 15 s in ms")
        self.assertEqual(log[1], ("sleep", ch.MENU_PAUSE))
        # a toast OR an error dialog ends the wait: a Validation Error shows a dialog and no toast ever
        self.assertEqual(log[3], ("wait_sel", ".o_notification, .o_error_dialog", "visible", 90000.0))
        self.assertEqual(log[5], ("sleep", ch.TOAST_SETTLE))

    def test_targets_the_visible_button_in_scope_with_the_whole_label_case_insensitive(self):
        (steps,) = clicks(run(ch.press_button, "Test Call"))
        self.assertEqual(selector_of(steps), ".o_form_view button:visible")
        flt = next(s for s in steps if s[0] == "filter")
        self.assertEqual(flt[1].pattern, ch.exact_text_re("Test Call").pattern)
        self.assertTrue(flt[1].flags & re.IGNORECASE, "DOM 'Test call' vs shown 'Test Call'")
        self.assertEqual(steps[-1], ("first",))

    def test_scope_is_configurable(self):
        (steps,) = clicks(run(ch.press_button, "Save", scope=".o_list_view"))
        self.assertEqual(selector_of(steps), ".o_list_view button:visible")

    def test_navigating_button_clicks_only_without_any_toast_wait(self):
        log = run(ch.press_button, "Transactions", wait_toast=False)
        self.assertEqual(self.kinds(log), ["sleep", "click"])
        self.assertEqual(log[0], ("sleep", ch.MENU_PAUSE))

    def test_custom_timeouts_pause_and_settle_are_honoured(self):
        log = run(ch.press_button, "Go", pause=0.1, settle=0.2, toast_timeout=5, prev_timeout=2)
        self.assertEqual(log[0][2], 2000)
        self.assertEqual(log[1], ("sleep", 0.1))
        self.assertEqual(log[3], ("wait_sel", ".o_notification, .o_error_dialog", "visible", 5000))
        self.assertEqual(log[5], ("sleep", 0.2))

    def test_hands_click_box_the_same_page_cursor_and_defaults(self):
        _, calls, page, cursor, error = run_calls(ch.press_button, "Go")
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], page)
        self.assertIs(calls[0][1], cursor)
        self.assertEqual(calls[0][2], {})

    def test_a_stuck_old_toast_is_reported_to_stderr_and_the_click_still_happens(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            log, calls, _, _, error = run_calls(ch.press_button, "Go", wait_fn_error=TimeoutError("t"))
        self.assertIsNone(error)
        self.assertEqual(len(calls), 1, "the click must still happen")
        self.assertIn("previous toast still shown", err.getvalue())
        self.assertIn("'Go'", err.getvalue())

    def test_a_new_toast_that_never_comes_raises_and_skips_the_settle(self):
        log, calls, _, _, error = run_calls(ch.press_button, "Go", wait_sel_error=TimeoutError("no toast"))
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(len(calls), 1)
        self.assertEqual([e for e in log if e[0] == "sleep"], [("sleep", ch.MENU_PAUSE)], "no settle after a failure")

    def test_a_failing_click_means_no_wait_for_a_toast(self):
        log, _, _, _, error = run_calls(ch.press_button, "Go", fail_on_click=1)
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIn("wait_sel", self.kinds(log))

    # --- error dialog instead of a toast (a masked take saved a real record behind a Validation Error) ---

    def test_error_dialog_constant_is_the_odoo17_content_class(self):
        # core/errors/error_dialogs.xml: <Dialog contentClass="'o_error_dialog'"> for every error dialog
        self.assertEqual(ch.ERROR_DIALOG, ".o_error_dialog")

    def test_no_dialog_probes_exactly_the_error_dialog_selector_and_returns(self):
        log, _, _, _, error = run_calls(ch.press_button, "Go")
        self.assertIsNone(error)
        (probe,) = [e for e in log if e[0] == "count"]
        self.assertEqual(probe[1], (("locator", ".o_error_dialog"),))
        self.assertNotIn("inner_text", self.kinds(log), "text is read only when a dialog is there")

    def test_an_error_dialog_instead_of_a_toast_raises_runtime_error_with_label_and_text(self):
        log, _, _, _, error = run_calls(ch.press_button, "Test Call", dialog_count=1,
                                        dialog_text="Validation Error\nThe operation cannot be completed")
        self.assertIsInstance(error, RuntimeError)
        msg = str(error)
        self.assertIn("press_button('Test Call')", msg)
        self.assertIn("error dialog appeared instead of a toast", msg)
        self.assertIn("The operation cannot be completed", msg)
        (read,) = [e for e in log if e[0] == "inner_text"]
        self.assertEqual(read[1], (("locator", ".o_error_dialog"), ("first",)),
                         "several dialogs may be stacked: read the first one only (strict mode)")

    def test_an_error_dialog_skips_the_settle_hold(self):
        log, calls, _, _, error = run_calls(ch.press_button, "Go", dialog_count=1, dialog_text="boom")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(len(calls), 1)
        self.assertEqual([e for e in log if e[0] == "sleep"], [("sleep", ch.MENU_PAUSE)],
                         "no TOAST_SETTLE hold after an error dialog")

    def test_dialog_text_is_stripped_and_cut_to_300_chars(self):
        _, _, _, _, error = run_calls(ch.press_button, "Go", dialog_count=1, dialog_text="  \n" + "x" * 400 + "\n  ")
        msg = str(error)
        self.assertIn(repr("x" * 300), msg)
        self.assertNotIn("x" * 301, msg)

    def test_several_dialogs_still_raise_once(self):
        _, _, _, _, error = run_calls(ch.press_button, "Go", dialog_count=3, dialog_text="boom")
        self.assertIsInstance(error, RuntimeError)

    def test_wait_toast_false_never_probes_for_a_dialog(self):
        log, _, _, _, error = run_calls(ch.press_button, "Go", wait_toast=False, dialog_count=1, dialog_text="boom")
        self.assertIsNone(error, "a navigating button does not wait, so it cannot judge an error dialog")
        self.assertEqual(self.kinds(log), ["sleep", "click"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
