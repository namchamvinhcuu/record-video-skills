#!/usr/bin/env python3
"""Test phần PURE của build_srt.py (parse_marks / build_cues / format_srt) — không cần
file .srt/.json thật trên đĩa.

Chạy: python3 shared/skills/record-demo-video/scripts/tests/test_build_srt.py
"""
from __future__ import annotations
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "build_srt.py"

spec = importlib.util.spec_from_file_location("build_srt_mod", SCRIPT)
assert spec and spec.loader
bs = importlib.util.module_from_spec(spec)
# BẮT BUỘC đăng ký vào sys.modules TRƯỚC exec_module: build_srt.py dùng
# `@dataclass` + `from __future__ import annotations` (postponed evaluation) — dataclass
# resolve type hint bằng cách tra `sys.modules[cls.__module__]`, thiếu bước này crash
# `AttributeError: 'NoneType' object has no attribute '__dict__'` (đã verify thực nghiệm).
sys.modules[spec.name] = bs
spec.loader.exec_module(bs)


class TestParseMarks(unittest.TestCase):
    def test_parses_valid_lines_ignores_noise(self):
        log = "\n".join([
            "some ffmpeg noise line",
            "MARK 1.50 open_login",
            "[Playwright] navigating...",
            "MARK 3.25 click_config_menu",
        ])
        marks = bs.parse_marks(log)
        self.assertEqual(marks, [bs.Mark(1.50, "open_login"), bs.Mark(3.25, "click_config_menu")])

    def test_empty_log_returns_empty_list(self):
        self.assertEqual(bs.parse_marks(""), [])


class TestBuildCues(unittest.TestCase):
    def setUp(self):
        self.marks = [
            bs.Mark(1.0, "step_a"),
            bs.Mark(4.0, "step_b"),
            bs.Mark(6.0, "step_c"),
        ]
        self.narration = {
            "step_a": {"vi": "Bước một", "ko": "1단계"},
            "step_b": {"vi": "Bước hai"},
            # step_c: cố tình thiếu narration → phải bị bỏ qua, không raise
        }

    def test_cue_spans_to_next_mark(self):
        cues = bs.build_cues(self.marks, self.narration, "vi")
        self.assertEqual(len(cues), 2)  # step_c bị skip vì thiếu narration
        self.assertEqual(cues[0].start, 1.0)
        self.assertEqual(cues[0].end, 4.0)
        self.assertEqual(cues[0].text, "Bước một")

    def test_last_cue_uses_fixed_duration(self):
        narration = {**self.narration, "step_c": {"vi": "Bước ba"}}
        cues = bs.build_cues(self.marks, narration, "vi")
        last = cues[-1]
        self.assertEqual(last.start, 6.0)
        self.assertEqual(last.end, 6.0 + bs.LAST_CUE_DURATION)

    def test_offset_shifts_all_timestamps(self):
        cues = bs.build_cues(self.marks, self.narration, "vi", offset=-0.5)
        self.assertEqual(cues[0].start, 0.5)

    def test_offset_never_goes_negative(self):
        cues = bs.build_cues(self.marks, self.narration, "vi", offset=-10.0)
        self.assertEqual(cues[0].start, 0.0)

    def test_missing_lang_falls_through_silently(self):
        cues = bs.build_cues(self.marks, self.narration, "ko")
        self.assertEqual(len(cues), 1)  # chỉ step_a có bản ko
        self.assertEqual(cues[0].text, "1단계")

    def test_empty_marks_returns_empty_list(self):
        self.assertEqual(bs.build_cues([], self.narration, "vi"), [])

    def test_empty_narration_returns_empty_list(self):
        # narration hoàn toàn rỗng (khác test_missing_lang: ở đây KHÔNG step nào có
        # bất kỳ bản dịch nào) — mọi mark phải bị skip, không raise.
        self.assertEqual(bs.build_cues(self.marks, {}, "vi"), [])

    def test_end_never_goes_below_start_plus_min_gap(self):
        # Mark kế tiếp trùng/thấp hơn mark hiện tại (elapsed không tăng) — guard
        # `max(start + 0.1, ...)` phải giữ cue tối thiểu 0.1s, không cho end <= start.
        marks = [bs.Mark(5.0, "step_x"), bs.Mark(5.0, "step_y")]
        narration = {"step_x": {"vi": "X"}}
        cues = bs.build_cues(marks, narration, "vi")
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0].start, 5.0)
        self.assertEqual(cues[0].end, 5.1)


class TestParseMarksInterleaved(unittest.TestCase):
    """Characterization: the pre-fix parser already split on `\\r` (str.splitlines), so `\\r`-glued marks
    were never lost HERE — in a customer's merged log it was the shell `grep ^MARK` that missed
    them. The first two tests pin that the finditer design keeps finding `\\r`-glued marks, in order; the
    others pin what the widened regex accepts (a non-word prefix) and rejects (noise)."""

    def test_mark_glued_after_ffmpeg_cr_progress_is_found(self):
        log = "frame=  812 fps= 25 q=28.0 size=    1024kB time=00:00:32.48 bitrate= 258.3kbits/s\rMARK 12.30 open_form\n"
        self.assertEqual(bs.parse_marks(log), [bs.Mark(12.30, "open_form")])

    def test_marks_keep_order_of_appearance_across_cr_and_lines(self):
        log = ("MARK 1.00 a\n"
               "frame=  10 fps=25\rframe=  20 fps=25\rMARK 2.50 b\rframe=  30 fps=25\n"
               "MARK 3.00 c\rMARK 3.40 d\r\n"            # two marks on one physical line, CRLF end
               "[Playwright] goto\n"
               "frame= 99\rMARK 9 e\n")                  # integer seconds still accepted
        self.assertEqual([(m.elapsed, m.step) for m in bs.parse_marks(log)],
                         [(1.0, "a"), (2.5, "b"), (3.0, "c"), (3.4, "d"), (9.0, "e")])

    def test_step_stops_at_cr_and_at_whitespace(self):
        marks = bs.parse_marks("MARK 4.20 save_order\rframe= 1\nMARK 5.00 open_login extra words\n")
        self.assertEqual([m.step for m in marks], ["save_order", "open_login"])

    def test_noise_that_is_not_a_mark_token_is_ignored(self):
        # Decision: MARK must stand alone as a word — a letter/digit/underscore or a '.' right before
        # it (XMARK, 1.MARK, _MARK) is part of another token; MARKS / MARK:, a missing step, a value
        # spread over two lines, a negative or dotted-twice value are not marks either.
        for noise in ("XMARK 1.00 a", "1.MARK 1.00 a", "_MARK 1.00 a", "MARKS 1.00 a", "MARK: 1.00 a",
                      "MARK 1.00", "MARK 1.00\nnext_line", "MARK\n1.00 a", "MARK -1.00 a", "MARK 1.2.3 a",
                      "MARK .50 a", "mark 1.00 a", "frame= 812 fps= 25 q=28.0 time=00:00:32.48"):
            with self.subTest(noise=noise):
                self.assertEqual(bs.parse_marks(noise), [])

    def test_mark_after_a_non_word_prefix_is_found(self):
        # A logger prefix (`[out] `, `: `, `\t`) is not part of the token — still a mark.
        for line, want in (("[out] MARK 1.50 a", 1.5), ("12:00:01:MARK 2.00 a", 2.0), ("\tMARK 3.00 a", 3.0)):
            with self.subTest(line=line):
                self.assertEqual(bs.parse_marks(line), [bs.Mark(want, "a")])


class TestCueSeconds(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(bs.READ_CPS, 16.0)
        self.assertEqual(bs.LAST_CUE_DURATION, 3.0)

    def test_short_or_empty_text_gets_min_cue(self):
        for text in ("", "OK", "x" * 47):
            with self.subTest(n=len(text)):
                self.assertEqual(bs.cue_seconds(text), 3.0)

    def test_boundary_at_min_cue_times_cps(self):
        self.assertEqual(bs.cue_seconds("x" * 48), 3.0)            # 48 / 16 == 3.0 exactly
        self.assertEqual(bs.cue_seconds("x" * 49), 49 / 16)        # one char more → longer
        self.assertEqual(bs.cue_seconds("x" * 160), 10.0)

    def test_counts_characters_not_bytes(self):
        text = "Bước một: mở phiếu kiểm tra chất lượng và lưu lại"   # 49 chars, more UTF-8 bytes
        self.assertEqual(len(text), 49)
        self.assertEqual(bs.cue_seconds(text), 49 / 16)

    def test_custom_min_cue_and_cps(self):
        self.assertEqual(bs.cue_seconds("x" * 20, min_cue=1.0, cps=10.0), 2.0)
        self.assertEqual(bs.cue_seconds("x" * 5, min_cue=1.0, cps=10.0), 1.0)


class TestLastCueReadingTime(unittest.TestCase):
    def test_long_last_cue_lasts_its_reading_time(self):
        text = "x" * 80                                            # 80 / 16 = 5 s > 3 s
        cues = bs.build_cues([bs.Mark(1.0, "a"), bs.Mark(6.0, "b")], {"a": {"vi": "A"}, "b": {"vi": text}}, "vi")
        self.assertEqual((cues[-1].start, cues[-1].end), (6.0, 11.0))

    def test_last_cue_reading_time_with_offset(self):
        cues = bs.build_cues([bs.Mark(10.0, "b")], {"b": {"vi": "y" * 64}}, "vi", offset=-2.5)
        self.assertEqual((cues[0].start, cues[0].end), (7.5, 11.5))

    def test_short_last_cue_keeps_the_3s_floor(self):
        cues = bs.build_cues([bs.Mark(2.0, "b")], {"b": {"vi": "Lưu"}}, "vi")
        self.assertEqual(cues[0].end, 5.0)

    def test_middle_cue_still_ends_at_the_next_mark_even_when_long(self):
        # build_srt does not stretch middle cues (that would overlap the next one) — pacing is the
        # recorder's job (mark_paced). Only the LAST cue has no next mark to end at.
        cues = bs.build_cues([bs.Mark(1.0, "a"), bs.Mark(2.0, "b")], {"a": {"vi": "z" * 160}, "b": {"vi": "B"}}, "vi")
        self.assertEqual((cues[0].start, cues[0].end), (1.0, 2.0))


class TestFormatSrt(unittest.TestCase):
    def test_timestamp_format(self):
        self.assertEqual(bs._srt_timestamp(3661.234), "01:01:01,234")
        self.assertEqual(bs._srt_timestamp(0), "00:00:00,000")

    def test_format_srt_block_structure(self):
        cues = [bs.Cue(1, 1.0, 4.0, "Bước một")]
        out = bs.format_srt(cues)
        self.assertIn("1\n00:00:01,000 --> 00:00:04,000\nBước một", out)


if __name__ == "__main__":
    unittest.main()
