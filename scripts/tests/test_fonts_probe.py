#!/usr/bin/env python3
"""Tests for fonts_probe.py — hermetic: no Windows registry, no ffmpeg (a fake `run` supplies probe output).

Covers the registry-name parser, the per-language font choice (Windows only, other platforms keep the caller's
default), the ffmpeg capability probe and the readable failure reason. Each test owns its data; nothing is
written to disk.

Run: python3 scripts/tests/test_fonts_probe.py
"""
from __future__ import annotations

import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import fonts_probe as fp  # noqa: E402
import burn_subtitles as bs  # noqa: E402

WIN_FONTS = {"Segoe UI", "Arial", "Malgun Gothic", "Tahoma"}


class TestRegistryNames(unittest.TestCase):
    def test_type_suffix_and_style_suffix_are_stripped(self):
        got = fp.families_from_registry_names(["Malgun Gothic Bold (TrueType)", "Segoe UI Semibold (TrueType)",
                                               "Arial (TrueType)", "Segoe UI Bold Italic (TrueType)"])
        self.assertTrue({"Malgun Gothic", "Segoe UI", "Arial", "Segoe UI Semibold"} <= got, got)

    def test_collection_names_are_split(self):
        got = fp.families_from_registry_names(["MS Gothic & MS UI Gothic & MS PGothic (TrueType)"])
        self.assertTrue({"MS Gothic", "MS UI Gothic", "MS PGothic"} <= got, got)

    def test_opentype_and_blank_and_empty(self):
        self.assertIn("Noto Sans CJK KR", fp.families_from_registry_names(["Noto Sans CJK KR Regular (OpenType)"]))
        self.assertEqual(fp.families_from_registry_names(["", "   (TrueType)"]), set())
        self.assertEqual(fp.families_from_registry_names([]), set())

    def test_windows_font_families_is_empty_where_there_is_no_registry(self):
        if sys.platform == "win32":
            self.skipTest("this machine HAS a registry")
        self.assertEqual(fp.windows_font_families(), set())


def fake_winreg(values_by_hive):
    """A stand-in for the Windows `winreg` module: {"HKLM": [value names], "HKCU": [...]}; a hive left out has
    no Fonts key (OpenKey raises OSError, as the real one does)."""
    mod = types.ModuleType("winreg")
    mod.HKEY_LOCAL_MACHINE, mod.HKEY_CURRENT_USER = "HKLM", "HKCU"

    class Key:
        def __init__(self, names):
            self.names = names

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(hive, path):
        if hive not in values_by_hive:
            raise OSError("no such key")
        return Key(values_by_hive[hive])

    def EnumValue(key, index):
        if index >= len(key.names):
            raise OSError("no more data")
        return (key.names[index], "file.ttf", 1)

    mod.OpenKey, mod.EnumValue = OpenKey, EnumValue
    return mod


class TestWindowsRegistryRead(unittest.TestCase):
    """The registry loop itself, driven by a fake `winreg` (the real one exists only on Windows)."""

    def read(self, values_by_hive):
        with mock.patch.dict(sys.modules, {"winreg": fake_winreg(values_by_hive)}):
            return fp.windows_font_families()

    def test_machine_and_user_fonts_are_merged(self):
        got = self.read({"HKLM": ["Segoe UI (TrueType)"], "HKCU": ["Malgun Gothic (TrueType)"]})
        self.assertTrue({"Segoe UI", "Malgun Gothic"} <= got, got)

    def test_a_missing_user_key_does_not_hide_the_machine_fonts(self):
        self.assertIn("Arial", self.read({"HKLM": ["Arial (TrueType)"]}))

    def test_no_font_keys_at_all_is_an_empty_set(self):
        self.assertEqual(self.read({}), set())

    def test_pick_font_end_to_end_through_the_fake_registry(self):
        with mock.patch.dict(sys.modules, {"winreg": fake_winreg({"HKLM": ["Malgun Gothic Bold (TrueType)"]})}):
            self.assertEqual(fp.pick_font("ko", "DEFAULT", platform="win32"), "Malgun Gothic")


class TestPickFont(unittest.TestCase):
    def pick(self, lang, installed, platform="win32", default="DEFAULT"):
        return fp.pick_font(lang, default, platform=platform, families_fn=lambda: installed)

    def test_ko_prefers_malgun_gothic(self):
        self.assertEqual(self.pick("ko", WIN_FONTS), "Malgun Gothic")

    def test_ko_falls_back_down_the_chain(self):
        self.assertEqual(self.pick("ko", {"Noto Sans CJK KR", "Arial"}), "Noto Sans CJK KR")

    def test_vi_prefers_segoe_ui_then_arial(self):
        self.assertEqual(self.pick("vi", WIN_FONTS), "Segoe UI")
        self.assertEqual(self.pick("vi", {"Arial"}), "Arial")

    def test_nothing_installed_returns_the_default(self):
        for lang in ("ko", "vi", "en", "xx"):
            with self.subTest(lang=lang):
                self.assertEqual(self.pick(lang, set()), "DEFAULT")

    def test_unknown_language_uses_the_fallback_chain(self):
        self.assertEqual(self.pick("xx", WIN_FONTS), "Segoe UI")

    def test_match_is_case_insensitive(self):
        self.assertEqual(self.pick("ko", {"MALGUN GOTHIC"}), "Malgun Gothic")

    def test_every_other_platform_keeps_the_default_and_never_probes(self):
        def boom():
            raise AssertionError("must not probe fonts off Windows")
        for platform in ("linux", "darwin", "freebsd14"):
            with self.subTest(platform=platform):
                self.assertEqual(fp.pick_font("ko", "DEFAULT", platform=platform, families_fn=boom), "DEFAULT")

    def test_every_font_in_every_chain_is_safe_in_the_ffmpeg_filter(self):
        chains = list(fp.FONT_CHAINS.values()) + [fp.FALLBACK_CHAIN]
        for chain in chains:
            for name in chain:
                with self.subTest(font=name):
                    self.assertFalse(bs._FILTER_UNSAFE & set(name), name)
                    flt = bs.build_filter("out_vi.srt", width=1920, height=1080, band=160, font=name,
                                          font_size=36, margin_v=55)
                    self.assertIn(f"FontName={name},", flt)


def fake_run(filters="", encoders="", exc=None, calls=None):
    """A `run` that answers `-filters` / `-encoders` with the given text (bytes, like a PIPE)."""
    def run(cmd, **kw):
        if calls is not None:
            calls.append(list(cmd))
        if exc is not None:
            raise exc
        text = filters if "-filters" in cmd else encoders
        return types.SimpleNamespace(returncode=0, stdout=text.encode("utf-8"))
    return run


FILTERS_WITH = " ... pad               V->V       Pad the input video.\n ... subtitles         V->V       Render text subtitles onto input video using the libass library.\n"
FILTERS_WITHOUT = " ... pad               V->V       Pad the input video.\n ... drawtext          V->V       Draw text on top of video frames.\n"
ENCODERS_WITH = " V....D libx264              libx264 H.264 / AVC (codec h264)\n"
ENCODERS_WITHOUT = " V....D mpeg4               MPEG-4 part 2\n"


class TestFfmpegFeatures(unittest.TestCase):
    def test_both_present(self):
        f = fp.ffmpeg_features(run=fake_run(FILTERS_WITH, ENCODERS_WITH))
        self.assertEqual((f["subtitles"], f["libx264"], f["error"]), (True, True, None))

    def test_libass_missing(self):
        f = fp.ffmpeg_features(run=fake_run(FILTERS_WITHOUT, ENCODERS_WITH))
        self.assertEqual((f["subtitles"], f["libx264"]), (False, True))

    def test_libx264_missing(self):
        f = fp.ffmpeg_features(run=fake_run(FILTERS_WITH, ENCODERS_WITHOUT))
        self.assertEqual((f["subtitles"], f["libx264"]), (True, False))

    def test_ffmpeg_not_startable(self):
        f = fp.ffmpeg_features(run=fake_run(exc=FileNotFoundError("ffmpeg")))
        self.assertIn("FileNotFoundError", f["error"])

    def test_timeout_is_reported_not_raised(self):
        f = fp.ffmpeg_features(run=fake_run(exc=subprocess.TimeoutExpired("ffmpeg", 30)))
        self.assertIn("TimeoutExpired", f["error"])

    def test_unreadable_output_is_unknown_not_missing(self):
        f = fp.ffmpeg_features(run=lambda cmd, **kw: types.SimpleNamespace(returncode=1))
        self.assertEqual((f["subtitles"], f["libx264"], f["error"]), (None, None, None))

    def test_a_probe_that_exits_non_zero_makes_no_claim_about_the_build(self):
        def run(cmd, **kw):
            return types.SimpleNamespace(returncode=3, stdout=b"Unrecognized option 'filters'\n")
        f = fp.ffmpeg_features(run=run)
        self.assertEqual((f["subtitles"], f["libx264"], f["error"]), (None, None, None))
        self.assertIsNone(fp.explain_ffmpeg_failure(run=run))

    def test_the_probe_runs_only_the_two_listing_commands(self):
        calls: list = []
        fp.ffmpeg_features("ffmpeg", run=fake_run(FILTERS_WITH, ENCODERS_WITH, calls=calls))
        self.assertEqual([c[-1] for c in calls], ["-filters", "-encoders"])


class TestExplainFailure(unittest.TestCase):
    def test_nothing_to_say_when_the_build_is_fine(self):
        self.assertIsNone(fp.explain_ffmpeg_failure(run=fake_run(FILTERS_WITH, ENCODERS_WITH)))

    def test_names_libass_and_the_windows_install_command(self):
        msg = fp.explain_ffmpeg_failure(run=fake_run(FILTERS_WITHOUT, ENCODERS_WITH))
        self.assertIn("libass", msg)
        self.assertIn("winget install Gyan.FFmpeg", msg)

    def test_names_both_when_both_are_missing(self):
        msg = fp.explain_ffmpeg_failure(run=fake_run(FILTERS_WITHOUT, ENCODERS_WITHOUT))
        self.assertIn("libass", msg)
        self.assertIn("libx264", msg)

    def test_ffmpeg_missing_is_readable(self):
        msg = fp.explain_ffmpeg_failure(run=fake_run(exc=FileNotFoundError("ffmpeg")))
        self.assertIn("cannot be started", msg)

    def test_an_unreadable_probe_makes_no_claim(self):
        self.assertIsNone(fp.explain_ffmpeg_failure(run=lambda cmd, **kw: types.SimpleNamespace(returncode=1)))


if __name__ == "__main__":
    unittest.main()
