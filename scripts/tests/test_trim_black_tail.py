#!/usr/bin/env python3
"""Test phần PURE của trim_black_tail.py (find_black_cutoff) — không cần ffmpeg/PIL/video thật.

Chạy: python3 shared/skills/record-demo-video/scripts/tests/test_trim_black_tail.py
"""
from __future__ import annotations
import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "trim_black_tail.py"

spec = importlib.util.spec_from_file_location("trim_black_tail_mod", SCRIPT)
assert spec and spec.loader
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)


class TestFindBlackCutoff(unittest.TestCase):
    def test_no_trailing_black_keeps_full_duration(self):
        samples = [(0.0, 200.0), (0.5, 210.0), (1.0, 190.0)]
        self.assertEqual(tb.find_black_cutoff(samples, threshold=10.0), 1.0)

    def test_trailing_black_run_trims_to_last_bright_frame(self):
        samples = [(0.0, 200.0), (0.5, 195.0), (1.0, 3.0), (1.5, 1.0), (2.0, 0.0)]
        # 3 sample cuối dưới ngưỡng 10 → cutoff phải là mốc sample sáng cuối cùng (0.5)
        self.assertEqual(tb.find_black_cutoff(samples, threshold=10.0), 0.5)

    def test_isolated_black_frame_in_middle_ignored(self):
        # frame đen thoáng qua GIỮA video (không liên tục tới cuối) không được cắt nhầm
        samples = [(0.0, 200.0), (0.5, 2.0), (1.0, 190.0), (1.5, 180.0)]
        self.assertEqual(tb.find_black_cutoff(samples, threshold=10.0), 1.5)

    def test_all_black_keeps_full_duration_not_zero(self):
        # REGRESSION cho bug 🔴 Critical:
        # trả 0.0 khi toàn bộ sample dưới ngưỡng từng làm `trim()` cắt video 3s xuống ~0.08s
        # (verify bằng ffmpeg thật) — samples[-1][0] LUÔN là mốc `duration` thật, phải giữ
        # nguyên chứ không phải hủy sạch video.
        samples = [(0.0, 1.0), (0.5, 2.0), (1.0, 0.0)]
        self.assertEqual(tb.find_black_cutoff(samples, threshold=10.0), 1.0)

    def test_trailing_black_longer_than_sample_window_keeps_full_duration(self):
        # Repro CHÍNH XÁC kịch bản gây bug: cửa sổ quét (vd 10s cuối) ĐEN HOÀN TOÀN vì đuôi
        # đen thật dài hơn cửa sổ — không được kết luận "cả video đen" rồi cắt về 0.
        samples = [(i * 0.5, 3.0) for i in range(21)] + [(10.3, 2.0)]
        self.assertEqual(tb.find_black_cutoff(samples, threshold=10.0), 10.3)

    def test_empty_samples_raises(self):
        with self.assertRaises(ValueError):
            tb.find_black_cutoff([], threshold=10.0)

    def test_threshold_zero_boundary_frame_counts_as_bright(self):
        # brightness == threshold phải PASS (>=, không phải >) — sample cuối brightness=0.0
        # đúng bằng threshold=0.0 nên KHÔNG bị coi là đen, cutoff giữ nguyên mốc cuối.
        samples = [(0.0, 50.0), (0.5, 0.0)]
        self.assertEqual(tb.find_black_cutoff(samples, threshold=0.0), 0.5)

    def test_negative_threshold_treats_everything_as_bright(self):
        samples = [(0.0, 3.0), (0.5, 0.0), (1.0, 0.0)]
        self.assertEqual(tb.find_black_cutoff(samples, threshold=-5.0), 1.0)


if __name__ == "__main__":
    unittest.main()
