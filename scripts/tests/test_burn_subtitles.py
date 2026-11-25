#!/usr/bin/env python3
"""Tests for burn_subtitles.py — no real ffmpeg: `subprocess.run` is patched.

Covers the pure argv/filter builder (exact argv, `-ss` BEFORE `-i` only when cut > 0,
pad/PlayRes math), `validate` (every rejection rule) and `main()` (default srt/out paths,
rc 2 paths, cwd = srt dir, sidecar warning).

Run: python3 shared/skills/record-demo-video/scripts/tests/test_burn_subtitles.py
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "burn_subtitles.py"
# burn_subtitles imports its sibling fonts_probe (a plain `python burn_subtitles.py` has the folder on sys.path).
if str(SCRIPT.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT.parent))

spec = importlib.util.spec_from_file_location("burn_subtitles_mod", SCRIPT)
assert spec and spec.loader
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

DEFAULT_STYLE = ("FontName=Noto Sans CJK KR,FontSize=36,PlayResX=1920,PlayResY=1240,"
                 "Alignment=2,MarginV=55,Outline=0,Shadow=0,BorderStyle=1")
ENCODE_TAIL = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart"]


class _TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name).resolve()

    def tearDown(self):
        self._td.cleanup()


class TestBuildFfmpegArgv(_TmpDirCase):
    def test_default_argv_with_cut_is_exact_and_ss_before_input(self):
        inp, srt, out = self.dir / "trimmed.mp4", self.dir / "out_vi.srt", self.dir / "final_vi.mp4"

        argv = bs.build_ffmpeg_argv(input_path=inp, srt=srt, out=out, cut=9)

        expected = (["ffmpeg", "-hide_banner", "-y", "-ss", "9.000", "-i", str(inp),
                     "-vf", f"pad=1920:1240:0:0:black,subtitles=out_vi.srt:force_style='{DEFAULT_STYLE}'"]
                    + ENCODE_TAIL + [str(out)])
        self.assertEqual(argv, expected)
        # -ss before -i keeps the filter clock starting at 0 after the cut (subtitle timing).
        self.assertLess(argv.index("-ss"), argv.index("-i"))

    def test_no_ss_when_cut_is_zero(self):
        argv = bs.build_ffmpeg_argv(input_path=self.dir / "a.mp4", srt=self.dir / "out_vi.srt",
                                    out=self.dir / "final_vi.mp4", cut=0)
        self.assertNotIn("-ss", argv)
        self.assertEqual(argv[:5], ["ffmpeg", "-hide_banner", "-y", "-i", str(self.dir / "a.mp4")])

    def test_fractional_cut_formatted_three_decimals(self):
        argv = bs.build_ffmpeg_argv(input_path=self.dir / "a.mp4", srt=self.dir / "s.srt",
                                    out=self.dir / "o.mp4", cut=0.5)
        i = argv.index("-ss")
        self.assertEqual(argv[i + 1], "0.500")
        self.assertEqual(argv[i + 2], "-i")

    def test_custom_geometry_font_size_margin(self):
        argv = bs.build_ffmpeg_argv(input_path=self.dir / "a.mp4", srt=self.dir / "s.srt",
                                    out=self.dir / "o.mp4", width=1280, height=720, band=100,
                                    font="Arial", font_size=28, margin_v=40)
        vf = argv[argv.index("-vf") + 1]
        self.assertEqual(
            vf,
            "pad=1280:820:0:0:black,subtitles=s.srt:force_style='FontName=Arial,FontSize=28,"
            "PlayResX=1280,PlayResY=820,Alignment=2,MarginV=40,Outline=0,Shadow=0,BorderStyle=1'")

    def test_relative_paths_are_resolved_but_filter_uses_srt_basename(self):
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            argv = bs.build_ffmpeg_argv(input_path=Path("a.mp4"), srt=Path("sub/out_ko.srt"),
                                        out=Path("final_ko.mp4"))
        finally:
            os.chdir(old)
        self.assertEqual(argv[argv.index("-i") + 1], str(self.dir / "a.mp4"))
        self.assertEqual(argv[-1], str(self.dir / "final_ko.mp4"))
        self.assertIn("subtitles=out_ko.srt:", argv[argv.index("-vf") + 1])

    def test_build_validates_first(self):
        with self.assertRaises(ValueError):
            bs.build_ffmpeg_argv(input_path=self.dir / "a.mp4", srt=self.dir / "s.srt",
                                 out=self.dir / "o.mp4", cut=-1)


class TestValidate(_TmpDirCase):
    def _ok(self, **over):
        kw = dict(srt=self.dir / "out_vi.srt", out=self.dir / "final_vi.mp4", cut=0.0,
                  height=1080, band=160, font="Noto Sans CJK KR")
        kw.update(over)
        return kw

    def test_valid_defaults_pass(self):
        self.assertIsNone(bs.validate(**self._ok()))

    def test_zero_cut_and_zero_band_allowed(self):
        bs.validate(**self._ok(cut=0, band=0))

    def test_negative_cut_rejected(self):
        with self.assertRaisesRegex(ValueError, "--cut"):
            bs.validate(**self._ok(cut=-0.1))

    def test_negative_band_rejected(self):
        with self.assertRaisesRegex(ValueError, "--band"):
            bs.validate(**self._ok(band=-2))

    def test_odd_height_plus_band_rejected(self):
        with self.assertRaisesRegex(ValueError, "1241"):
            bs.validate(**self._ok(band=161))
        with self.assertRaisesRegex(ValueError, "lẻ"):
            bs.validate(**self._ok(height=1081, band=160))

    def test_every_unsafe_char_in_srt_basename_rejected(self):
        for ch in "\\':,;[]=":
            with self.subTest(ch=ch), self.assertRaises(ValueError):
                bs.validate(**self._ok(srt=self.dir / f"out{ch}vi.srt"))

    def test_unsafe_char_in_srt_directory_is_allowed(self):
        # Only the basename reaches the filtergraph (ffmpeg runs with cwd = srt dir).
        bs.validate(**self._ok(srt=self.dir / "a,b=c" / "out_vi.srt"))

    def test_every_unsafe_char_in_font_rejected(self):
        for ch in "\\':,;[]=":
            with self.subTest(ch=ch), self.assertRaises(ValueError):
                bs.validate(**self._ok(font=f"Noto{ch}Sans"))

    def test_srt_equal_to_out_sidecar_rejected(self):
        with self.assertRaisesRegex(ValueError, "final_vi.srt"):
            bs.validate(**self._ok(srt=self.dir / "final_vi.srt"))

    def test_srt_equal_to_out_sidecar_rejected_through_relative_path(self):
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            with self.assertRaises(ValueError):
                bs.validate(**self._ok(srt=Path("final_vi.srt"), out=self.dir / "final_vi.mp4"))
        finally:
            os.chdir(old)

    def test_same_basename_in_other_dir_allowed(self):
        bs.validate(**self._ok(srt=self.dir / "subs" / "final_vi.srt"))


class TestMain(_TmpDirCase):
    def setUp(self):
        super().setUp()
        self.inp = self.dir / "trimmed.mp4"
        self.inp.write_bytes(b"")
        self.calls: list = []
        self.rc = 0

    def _fake_run(self, cmd, cwd=None, **kw):
        self.calls.append((list(cmd), cwd))
        return types.SimpleNamespace(returncode=self.rc)

    def _main(self, *args):
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(bs.subprocess, "run", side_effect=self._fake_run), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = bs.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_defaults_next_to_input_and_cwd_is_srt_dir(self):
        (self.dir / "out_vi.srt").write_text("1\n", encoding="utf-8")

        rc, _out, err = self._main("--input", str(self.inp), "--lang", "vi", "--cut", "9")

        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.calls), 1)
        cmd, cwd = self.calls[0]
        self.assertEqual(Path(cwd), self.dir)
        self.assertEqual(cmd[-1], str(self.dir / "final_vi.mp4"))
        self.assertEqual(cmd[cmd.index("-i") + 1], str(self.inp))
        self.assertIn("subtitles=out_vi.srt:", cmd[cmd.index("-vf") + 1])
        self.assertEqual(cmd[cmd.index("-ss") + 1], "9.000")
        self.assertEqual(err, "")

    def test_relative_input_resolves_defaults_against_its_dir(self):
        sub = self.dir / "clip"
        sub.mkdir()
        (sub / "trimmed.mp4").write_bytes(b"")
        (sub / "out_ko.srt").write_text("1\n", encoding="utf-8")
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            rc, _o, err = self._main("--input", "clip/trimmed.mp4", "--lang", "ko")
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0, err)
        cmd, cwd = self.calls[0]
        self.assertEqual(Path(cwd), sub)
        self.assertEqual(cmd[-1], str(sub / "final_ko.mp4"))
        self.assertNotIn("-ss", cmd)

    def test_explicit_srt_in_other_dir_sets_cwd_to_that_dir(self):
        subs = self.dir / "subs"
        subs.mkdir()
        (subs / "custom.srt").write_text("1\n", encoding="utf-8")
        rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi",
                                 "--srt", str(subs / "custom.srt"), "--out", str(self.dir / "x.mp4"))
        self.assertEqual(rc, 0, err)
        cmd, cwd = self.calls[0]
        self.assertEqual(Path(cwd), subs)
        self.assertEqual(cmd[-1], str(self.dir / "x.mp4"))

    def test_missing_srt_returns_2_without_running_ffmpeg(self):
        rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi")
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])
        self.assertIn("out_vi.srt", err)

    def test_value_error_returns_2_without_running_ffmpeg(self):
        (self.dir / "out_vi.srt").write_text("1\n", encoding="utf-8")
        rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi", "--cut", "-1")
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])
        self.assertTrue(err.startswith("burn_subtitles: "), err)

    def test_srt_as_out_sidecar_returns_2(self):
        (self.dir / "final_vi.srt").write_text("1\n", encoding="utf-8")
        rc, _o, _err = self._main("--input", str(self.inp), "--lang", "vi",
                                  "--srt", str(self.dir / "final_vi.srt"))
        self.assertEqual(rc, 2)
        self.assertEqual(self.calls, [])

    def test_ffmpeg_failure_rc_propagated(self):
        (self.dir / "out_vi.srt").write_text("1\n", encoding="utf-8")
        self.rc = 1
        rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi")
        self.assertEqual(rc, 1)
        self.assertIn("rc=1", err)

    def test_warns_when_out_sidecar_exists_after_run(self):
        (self.dir / "out_vi.srt").write_text("1\n", encoding="utf-8")
        (self.dir / "final_vi.srt").write_text("stale\n", encoding="utf-8")
        rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi")
        self.assertEqual(rc, 0)
        self.assertIn("CẢNH BÁO", err)
        self.assertIn("final_vi.srt", err)

    def test_no_warning_without_sidecar(self):
        (self.dir / "out_vi.srt").write_text("1\n", encoding="utf-8")
        _rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi")
        self.assertNotIn("CẢNH BÁO", err)

    # ---- per-language font (plugin addition: Windows picks an installed system font, elsewhere unchanged)
    def _font_of(self, *args):
        cmd, _cwd = self.calls[-1]
        return cmd[cmd.index("-vf") + 1].split("FontName=")[1].split(",")[0]

    def test_off_windows_the_default_font_is_used_and_nothing_is_probed(self):
        (self.dir / "out_ko.srt").write_text("1\n", encoding="utf-8")
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(sys.modules["fonts_probe"], "windows_font_families",
                                  side_effect=AssertionError("probed off Windows")):
            rc, _o, err = self._main("--input", str(self.inp), "--lang", "ko")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._font_of(), bs.DEFAULT_FONT)
        self.assertEqual((len(self.calls), err), (1, ""), "the success path runs ffmpeg once and prints nothing")

    def test_on_windows_the_installed_font_of_the_language_is_used(self):
        fonts = sys.modules["fonts_probe"]
        for lang, want in (("ko", "Malgun Gothic"), ("vi", "Segoe UI")):
            with self.subTest(lang=lang):
                (self.dir / f"out_{lang}.srt").write_text("1\n", encoding="utf-8")
                with mock.patch.object(sys, "platform", "win32"), \
                        mock.patch.object(fonts, "windows_font_families", return_value={"Malgun Gothic", "Segoe UI"}):
                    rc, _o, err = self._main("--input", str(self.inp), "--lang", lang)
                self.assertEqual(rc, 0, err)
                self.assertEqual(self._font_of(), want)

    def test_an_explicit_font_beats_the_windows_choice(self):
        fonts = sys.modules["fonts_probe"]
        (self.dir / "out_ko.srt").write_text("1\n", encoding="utf-8")
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(fonts, "windows_font_families", return_value={"Malgun Gothic"}):
            rc, _o, err = self._main("--input", str(self.inp), "--lang", "ko", "--font", "Gulim")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self._font_of(), "Gulim")

    def test_ffmpeg_not_installed_is_a_readable_message_not_a_traceback(self):
        (self.dir / "out_vi.srt").write_text("1\n", encoding="utf-8")
        err, out = io.StringIO(), io.StringIO()
        with mock.patch.object(bs.subprocess, "run", side_effect=FileNotFoundError("ffmpeg")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = bs.main(["--input", str(self.inp), "--lang", "vi"])
        self.assertEqual(rc, 2)
        self.assertIn("winget install Gyan.FFmpeg", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_ffmpeg_failure_adds_a_readable_reason_only_when_the_build_is_to_blame(self):
        (self.dir / "out_vi.srt").write_text("1\n", encoding="utf-8")
        self.rc = 1
        with mock.patch.object(bs, "explain_ffmpeg_failure", return_value="this ffmpeg build lacks libass"):
            rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi")
        self.assertEqual(rc, 1)
        self.assertIn("rc=1", err)
        self.assertIn("lacks libass", err)
        with mock.patch.object(bs, "explain_ffmpeg_failure", return_value=None):
            rc, _o, err = self._main("--input", str(self.inp), "--lang", "vi")
        self.assertEqual(rc, 1)
        self.assertNotIn("lacks", err)


if __name__ == "__main__":
    unittest.main()
