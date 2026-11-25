#!/usr/bin/env python3
"""trim_black_tail.py — tìm mốc thời gian video chuyển đen ở CUỐI (đã đóng tab/mất focus
sau khi dừng ghi) rồi cắt bỏ bằng ffmpeg. Logic tìm mốc (`find_black_cutoff`) tách PURE
khỏi việc extract frame để test không cần file video/ffmpeg thật. Xem REFERENCE.md §8.

Chạy: python trim_black_tail.py raw_capture.mp4 --out trimmed.mp4 [--threshold 10] [--interval 0.5]
"""
from __future__ import annotations

import argparse
import io
import subprocess


def find_black_cutoff(samples: list[tuple[float, float]], threshold: float) -> float:
    """samples: [(timestamp, mean_brightness_0_255), ...] SẮP XẾP TĂNG DẦN theo timestamp,
    phủ hết video. Trả về mốc thời gian cần GIỮ tới (ffmpeg -t <cutoff>) — quét ngược từ
    cuối, dừng ngay tại frame sáng đầu tiên gặp được (tự nhiên loại toàn bộ đuôi đen liên
    tục, kể cả khi đuôi đen không tuyệt đối đồng nhất giữa các sample)."""
    if not samples:
        raise ValueError("samples rỗng — không có gì để tìm cutoff")
    for ts, brightness in reversed(samples):
        if brightness >= threshold:
            return ts
    # Toàn bộ SAMPLE (thường chỉ phủ N giây CUỐI qua sample_tail_brightness, KHÔNG phải cả
    # video) đều dưới ngưỡng — KHÔNG được suy ra "cả video đen" rồi trả 0.0: verify thực
    # nghiệm bằng ffmpeg thật cho thấy `-t 0.000` cắt video 3s xuống còn ~0.08s, tức là
    # trim gần như XÓA SẠCH deliverable cho khách một cách im lặng khi đuôi đen dài hơn cửa
    # sổ quét. samples[-1][0] luôn là mốc `duration` thật (sample_tail_brightness luôn
    # append nó cuối cùng) → giữ nguyên toàn bộ video đúng như ý định "giữ nguyên, tránh
    # mất hết".
    return samples[-1][0]


def _probe_duration(video_path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _frame_brightness(video_path: str, timestamp: float) -> float:
    from PIL import Image  # import trễ — optional dep, chỉ cần khi thật sự chạy

    proc = subprocess.run(
        ["ffmpeg", "-ss", f"{timestamp:.3f}", "-i", video_path,
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, check=True,
    )
    img = Image.open(io.BytesIO(proc.stdout)).convert("L")  # grayscale
    pixels = list(img.getdata())
    return sum(pixels) / len(pixels)


def sample_tail_brightness(video_path: str, interval: float = 0.5,
                            tail_window: float = 10.0) -> list[tuple[float, float]]:
    """Sample brightness mỗi `interval` giây trong `tail_window` giây cuối video —
    không cần quét cả video, đoạn đen chỉ xảy ra ở cuối."""
    duration = _probe_duration(video_path)
    start = max(0.0, duration - tail_window)
    samples = []
    ts = start
    while ts < duration:
        samples.append((ts, _frame_brightness(video_path, ts)))
        ts += interval
    samples.append((duration, _frame_brightness(video_path, max(0.0, duration - 0.05))))
    return samples


def trim(video_path: str, out_path: str, cutoff: float) -> None:
    subprocess.run(
        ["ffmpeg", "-i", video_path, "-t", f"{cutoff:.3f}", "-c", "copy", "-y", out_path],
        check=True,
    )


if __name__ == "__main__":
    from text_io import force_utf8_stdio

    force_utf8_stdio()  # TRƯỚC argparse: --help và dòng "Cutoff = … → …" không mã hoá được bằng cp1252 trên Windows
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("video", help="video nguồn cần trim đuôi đen")
    ap.add_argument("--out", required=True, help="video đích sau khi trim")
    ap.add_argument("--threshold", type=float, default=10.0, help="ngưỡng sáng 0-255, dưới ngưỡng = đen (mặc định thực nghiệm)")
    ap.add_argument("--interval", type=float, default=0.5, help="khoảng cách giữa các sample (giây)")
    ap.add_argument("--tail-window", type=float, default=10.0, help="chỉ quét N giây cuối, không quét cả video")
    args = ap.parse_args()

    samples = sample_tail_brightness(args.video, interval=args.interval, tail_window=args.tail_window)
    cutoff = find_black_cutoff(samples, args.threshold)
    trim(args.video, args.out, cutoff)
    print(f"Cutoff = {cutoff:.2f}s (từ {len(samples)} sample, threshold={args.threshold}) → {args.out}")
