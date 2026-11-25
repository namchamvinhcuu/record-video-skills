#!/usr/bin/env python3
"""tour_helpers.py (fill_narration / format_number / mask_text_js / mask_input_overlay_js script text /
assert_form_clean) — pure functions and a locator twin, no browser (the in-page behaviour is in
test_tour_helpers_real_chrome.py).

Run: python3 shared/skills/record-demo-video/scripts/tests/test_tour_helpers.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("tour_helpers_mod", HERE.parent / "tour_helpers.py")
assert _spec and _spec.loader
th = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(th)

NARR = {
    "home": {"vi": "Màn hình chính.", "en": "Home."},
    "click_app": {"vi": "Bước {S}: bấm app.", "en": "Step {S}: click the app."},
    "view": {"vi": "Đây là danh sách.", "en": "This is the list."},
    "click_menu": {"vi": "Bước {S}: bấm menu.", "en": "Step {S}: click the menu."},
}


class TestFormatNumber(unittest.TestCase):
    def test_vi_uses_dots_every_other_language_uses_commas(self):
        self.assertEqual(th.format_number(1000000, "vi"), "1.000.000")
        self.assertEqual(th.format_number(1000000, "en"), "1,000,000")
        self.assertEqual(th.format_number(1000000, "ko"), "1,000,000")

    def test_small_numbers_have_no_separator(self):
        self.assertEqual(th.format_number(999, "vi"), "999")
        self.assertEqual(th.format_number(0, "en"), "0")


class TestFillNarrationSteps(unittest.TestCase):
    def test_only_steps_with_the_token_are_counted_in_order(self):
        out = th.fill_narration(NARR)
        self.assertEqual(out["click_app"]["vi"], "Bước 1: bấm app.")
        self.assertEqual(out["click_menu"]["en"], "Step 2: click the menu.")

    def test_the_same_number_in_every_language(self):
        out = th.fill_narration(NARR)
        for step in ("click_app", "click_menu"):
            self.assertEqual(re.findall(r"\d+", out[step]["vi"]), re.findall(r"\d+", out[step]["en"]))

    def test_steps_without_the_token_are_untouched_and_not_counted(self):
        out = th.fill_narration(NARR)
        self.assertEqual(out["home"], NARR["home"])
        self.assertEqual(out["view"], NARR["view"])

    def test_inserting_a_scene_renumbers_the_rest_by_itself(self):
        more = {"a": {"vi": "Bước {S}", "en": "Step {S}"}, "new": {"vi": "Bước {S}", "en": "Step {S}"},
                "b": {"vi": "Bước {S}", "en": "Step {S}"}}
        out = th.fill_narration(more)
        self.assertEqual([out[k]["en"] for k in ("a", "new", "b")], ["Step 1", "Step 2", "Step 3"])

    def test_input_is_not_mutated_and_key_order_is_kept(self):
        before = json.dumps(NARR, sort_keys=False)
        out = th.fill_narration(NARR)
        self.assertEqual(json.dumps(NARR, sort_keys=False), before)
        self.assertEqual(list(out), list(NARR))
        self.assertIsNot(out, NARR)

    def test_a_step_with_the_token_in_only_one_language_still_counts(self):
        out = th.fill_narration({"a": {"vi": "Bước {S}", "en": "Click"}, "b": {"vi": "Bước {S}", "en": "Step {S}"}})
        self.assertEqual(out["a"], {"vi": "Bước 1", "en": "Click"})
        self.assertEqual(out["b"], {"vi": "Bước 2", "en": "Step 2"})

    def test_custom_token(self):
        out = th.fill_narration({"a": {"en": "Step #N"}, "b": {"en": "Step #N"}}, step_token="#N")
        self.assertEqual([out["a"]["en"], out["b"]["en"]], ["Step 1", "Step 2"])


class TestFillNarrationValues(unittest.TestCase):
    def test_per_language_values(self):
        narr = {"r": {"vi": "Số dư {b0}.", "en": "Balance {b0}."}}
        vals = {"b0": {"vi": th.format_number(1000000, "vi"), "en": th.format_number(1000000, "en")}}
        out = th.fill_narration(narr, vals)
        self.assertEqual(out["r"], {"vi": "Số dư 1.000.000.", "en": "Balance 1,000,000."})

    def test_plain_text_value_applies_to_every_language(self):
        out = th.fill_narration({"r": {"vi": "Tài khoản {acc}", "en": "Account {acc}"}}, {"acc": "ABC"})
        self.assertEqual(out["r"], {"vi": "Tài khoản ABC", "en": "Account ABC"})

    def test_a_non_string_value_is_stringified(self):
        out = th.fill_narration({"r": {"en": "{n} rows"}}, {"n": 12})
        self.assertEqual(out["r"]["en"], "12 rows")

    def test_a_forgotten_value_raises_naming_step_language_and_placeholder(self):
        with self.assertRaises(ValueError) as cm:
            th.fill_narration({"r": {"vi": "Số dư {b1}"}}, {"b0": "1"})
        msg = str(cm.exception)
        self.assertIn("'r'", msg)
        self.assertIn("vi", msg)
        self.assertIn("{b1}", msg)

    def test_leftover_names_may_start_with_an_underscore_or_hold_digits(self):
        for left in ("{_tmp}", "{b_1}", "{x1}"):
            with self.assertRaises(ValueError, msg=left):
                th.fill_narration({"r": {"en": f"Total {left}"}})

    def test_text_that_is_not_a_placeholder_is_left_alone(self):
        # braces with a space / empty / a leading digit are prose or code, not a forgotten value
        out = th.fill_narration({"r": {"en": "Use {} or { x } or {1} here"}})
        self.assertEqual(out["r"]["en"], "Use {} or { x } or {1} here")

    def test_a_per_language_value_missing_a_language_raises(self):
        with self.assertRaises(KeyError):
            th.fill_narration({"r": {"vi": "{b0}", "ko": "{b0}"}}, {"b0": {"vi": "1"}})

    def test_unused_values_are_fine(self):
        out = th.fill_narration({"r": {"en": "hi"}}, {"never": "x"})
        self.assertEqual(out["r"]["en"], "hi")

    def test_no_values_and_no_tokens_is_an_identity_copy(self):
        self.assertEqual(th.fill_narration({"r": {"en": "hi"}}), {"r": {"en": "hi"}})


class TestMaskTextJs(unittest.TestCase):
    def test_pattern_and_replacement_are_embedded_as_json_string_literals(self):
        js = th.mask_text_js(r"\b5500\d{4}(\d{4})\b", "********$1")
        self.assertIn(json.dumps(r"\b5500\d{4}(\d{4})\b"), js)
        self.assertIn(json.dumps("********$1"), js)

    def test_a_quote_or_newline_in_the_arguments_cannot_break_out_of_the_literal(self):
        js = th.mask_text_js('a"b\nc', "x'y\"z")
        self.assertIn(json.dumps('a"b\nc'), js)
        self.assertIn(json.dumps("x'y\"z"), js)

    def test_interval_default_and_custom(self):
        self.assertIn("setInterval(mask, 100)", th.mask_text_js("a", "b"))
        self.assertIn("setInterval(mask, 250)", th.mask_text_js("a", "b", interval_ms=250))

    def test_walks_text_nodes_only_and_waits_for_the_body(self):
        js = th.mask_text_js("a", "b")
        self.assertIn("NodeFilter.SHOW_TEXT", js)
        self.assertIn("if (!document.body) return;", js)

    def test_global_flag_so_every_match_in_a_node_is_masked(self):
        self.assertIn("'g'", th.mask_text_js("a", "b"))

    def test_observer_is_on_by_default_and_the_interval_stays_as_a_safety_net(self):
        for js in (th.mask_text_js("a", "b"), th.mask_text_js("a", "b", observe=True)):
            self.assertIn("new MutationObserver", js)
            self.assertIn("setInterval(mask, 100)", js)

    def test_observe_false_is_the_interval_only_script(self):
        js = th.mask_text_js("a", "b", interval_ms=250, observe=False)
        self.assertNotIn("MutationObserver", js)
        self.assertIn("setInterval(mask, 250)", js)


class TestMaskInputOverlayJs(unittest.TestCase):
    def test_pattern_replacement_and_selector_are_embedded_as_json_string_literals(self):
        js = th.mask_input_overlay_js('a"b\nc', "x'y\"z", selector="input[name='acc']")
        self.assertIn(json.dumps('a"b\nc'), js)
        self.assertIn(json.dumps("x'y\"z"), js)
        self.assertIn(json.dumps("input[name='acc']"), js)

    def test_default_selector_and_interval(self):
        js = th.mask_input_overlay_js("a", "b")
        self.assertIn(json.dumps("input, textarea"), js)
        self.assertIn("setInterval(sync, 30)", js)
        self.assertIn("setInterval(sync, 250)", th.mask_input_overlay_js("a", "b", interval_ms=250))

    def test_global_flag_so_every_match_in_a_value_is_masked(self):
        self.assertIn("'g'", th.mask_input_overlay_js("a", "b"))


class FakeLocator:
    """What `page.locator(sel)` returns in assert_form_clean: only an awaited count()."""

    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def count(self):
        self.page.asked.append(self.selector)
        return self.page.n


class FakePage:
    def __init__(self, n):
        self.n, self.asked = n, []

    def locator(self, selector):
        return FakeLocator(self, selector)


class TestAssertFormClean(unittest.TestCase):
    def call(self, n, **kw):
        page = FakePage(n)
        error, result = None, "unset"
        try:
            result = asyncio.run(th.assert_form_clean(page, **kw))
        except Exception as exc:  # returned so the test can assert on type and message
            error = exc
        return page, result, error

    def test_default_selector_is_the_odoo17_save_discard_indicator_that_is_not_invisible(self):
        # form_status_indicator.xml: class "o_form_status_indicator_buttons d-flex", `invisible` when clean
        self.assertEqual(th.FORM_DIRTY_SELECTOR, ".o_form_status_indicator_buttons:not(.invisible)")

    def test_a_clean_form_returns_none(self):
        page, result, error = self.call(0)
        self.assertIsNone(error)
        self.assertIsNone(result)
        self.assertEqual(page.asked, [th.FORM_DIRTY_SELECTOR])

    def test_a_dirty_form_raises_runtime_error_saying_why(self):
        _, _, error = self.call(1)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("unsaved changes", str(error))

    def test_any_positive_count_is_dirty(self):
        for n in (2, 5):
            _, _, error = self.call(n)
            self.assertIsInstance(error, RuntimeError, n)

    def test_a_custom_selector_reaches_locator(self):
        page, _, error = self.call(1, selector=".my_dirty")
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(page.asked, [".my_dirty"])
        page, result, error = self.call(0, selector="#other")
        self.assertIsNone(error)
        self.assertEqual(page.asked, ["#other"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
