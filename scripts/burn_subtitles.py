#!/usr/bin/env python3
"""burn_subtitles.py — burn phụ đề 1 ngôn ngữ vào dải đen pad DƯỚI khung capture, cắt
đoạn loading đầu video. Công thức mặc định đã được duyệt trên video mẫu vi/ko
(2026-10-16) — xem REFERENCE.md §7, đừng gõ lại filter bằng tay.

- Mỗi ngôn ngữ 1 video riêng, không xếp chồng 2 dòng song ngữ.
- Phụ đề nằm trong dải đen `--band` px thêm dưới khung → không che UI.
  PlayResY PHẢI = chiều cao SAU pad (height + band), không phải 1080.
- `-ss <cut>` đặt TRƯỚC `-i`: filter thấy timestamp bắt đầu từ 0 sau khi cắt, nên SRT
  build với offset = -(cut) - 0.3 là khớp. Đặt `-ss` SAU `-i` thì filter thấy timestamp
  gốc → phụ đề lệch/mất (đo 2026-10-19 trên clip testsrc).
- KHÔNG để `.srt` cùng basename cạnh `.mp4` giao đi: player tự nạp nó và vẽ thêm một lớp
  phụ đề thứ hai khổng lồ.

Logic tách PURE (build_ffmpeg_argv / validate) khỏi I/O để test không cần ffmpeg.

Chạy: python burn_subtitles.py --input trimmed.mp4 --lang vi --cut 9
      (mặc định đọc out_vi.srt, ghi final_vi.mp4)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from fonts_probe import explain_ffmpeg_failure, pick_font

DEFAULT_FONT = "Noto Sans CJK KR"   # có đủ glyph Việt + Hàn
DEFAULT_BAND = 160
DEFAULT_FONT_SIZE = 36
DEFAULT_MARGIN_V = 55

# Ký tự phá cú pháp filtergraph / force_style của ffmpeg nếu nằm trong tên file hoặc font.
# Chạy ffmpeg với cwd = thư mục chứa .srt và chỉ truyền basename, nên chỉ cần chặn ở basename.
_FILTER_UNSAFE = set("\\':,;[]=")


def play_res_y(height: int, band: int) -> int:
    return height + band


def validate(*, srt: Path, out: Path, cut: float, height: int, band: int, font: str) -> None:
    """Raise ValueError với thông báo rõ ràng khi tham số sẽ cho ra video sai."""
    if cut < 0:
        raise ValueError(f"--cut phải >= 0 (nhận {cut})")
    if band < 0:
        raise ValueError(f"--band phải >= 0 (nhận {band})")
    if play_res_y(height, band) % 2:
        raise ValueError(f"height + band = {play_res_y(height, band)} lẻ — yuv420p cần kích thước chẵn")
    bad = _FILTER_UNSAFE & set(srt.name)
    if bad:
        raise ValueError(f"tên file .srt chứa ký tự phá filtergraph {sorted(bad)}: {srt.name} — đổi tên file")
    bad = _FILTER_UNSAFE & set(font)
    if bad:
        raise ValueError(f"tên font chứa ký tự phá force_style {sorted(bad)}: {font}")
    if srt.resolve() == out.resolve().with_suffix(".srt"):
        raise ValueError(f"{srt.name} trùng basename với {out.name} — player sẽ tự nạp nó thành lớp phụ đề thứ hai; "
                         "đặt tên mp4 khác basename .srt (mặc định: out_<lang>.srt → final_<lang>.mp4)")


def build_filter(srt_name: str, *, width: int, height: int, band: int, font: str,
                 font_size: int, margin_v: int) -> str:
    total_h = play_res_y(height, band)
    style = ",".join([
        f"FontName={font}", f"FontSize={font_size}",
        f"PlayResX={width}", f"PlayResY={total_h}",
        "Alignment=2", f"MarginV={margin_v}",
        "Outline=0", "Shadow=0", "BorderStyle=1",
    ])
    return f"pad={width}:{total_h}:0:0:black,subtitles={srt_name}:force_style='{style}'"


def build_ffmpeg_argv(*, input_path: Path, srt: Path, out: Path, cut: float = 0.0,
                      width: int = 1920, height: int = 1080, band: int = DEFAULT_BAND,
                      font: str = DEFAULT_FONT, font_size: int = DEFAULT_FONT_SIZE,
                      margin_v: int = DEFAULT_MARGIN_V) -> list[str]:
    """argv ffmpeg; phải chạy với cwd = srt.parent (filter chỉ nhận basename .srt)."""
    validate(srt=srt, out=out, cut=cut, height=height, band=band, font=font)
    argv = ["ffmpeg", "-hide_banner", "-y"]
    if cut > 0:
        argv += ["-ss", f"{cut:.3f}"]          # TRƯỚC -i — xem docstring module
    argv += [
        "-i", str(input_path.resolve()),
        "-vf", build_filter(srt.name, width=width, height=height, band=band, font=font,
                            font_size=font_size, margin_v=margin_v),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out.resolve()),
    ]
    return argv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="video đã trim đuôi đen (vd trimmed.mp4)")
    ap.add_argument("--lang", required=True, help="mã ngôn ngữ — mặc định srt=out_<lang>.srt, out=final_<lang>.mp4")
    ap.add_argument("--srt", help="file .srt (mặc định out_<lang>.srt cạnh --input)")
    ap.add_argument("--out", help="mp4 xuất ra (mặc định final_<lang>.mp4 cạnh --input)")
    ap.add_argument("--cut", type=float, default=0.0,
                    help="giây cắt bỏ ở đầu (đoạn loading); build_srt.py --offset phải = -(cut) - 0.3")
    ap.add_argument("--band", type=int, default=DEFAULT_BAND, help="chiều cao dải đen phụ đề (px, chẵn)")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080, help="chiều cao capture gốc (trước pad)")
    ap.add_argument("--font", default=None,
                    help=f"tên font (mặc định {DEFAULT_FONT}; trên Windows: font đầu chuỗi của --lang đang cài, "
                         "vd ko → Malgun Gothic, vi → Segoe UI)")
    ap.add_argument("--font-size", type=int, default=DEFAULT_FONT_SIZE)
    ap.add_argument("--margin-v", type=int, default=DEFAULT_MARGIN_V)
    args = ap.parse_args(argv)

    input_path = Path(args.input)
    base = input_path.resolve().parent
    srt = Path(args.srt) if args.srt else base / f"out_{args.lang}.srt"
    out = Path(args.out) if args.out else base / f"final_{args.lang}.mp4"
    # Chọn font trong main(), không trong build_*: ngoài Windows trả đúng DEFAULT_FONT, không subprocess, không stderr.
    font = args.font or pick_font(args.lang, DEFAULT_FONT)
    try:
        cmd = build_ffmpeg_argv(input_path=input_path, srt=srt, out=out, cut=args.cut,
                                width=args.width, height=args.height, band=args.band,
                                font=font, font_size=args.font_size, margin_v=args.margin_v)
    except ValueError as e:
        print(f"burn_subtitles: {e}", file=sys.stderr)
        return 2
    if not srt.is_file():
        print(f"burn_subtitles: không thấy file phụ đề {srt}", file=sys.stderr)
        return 2

    print(f"burn_subtitles: {srt.name} → {out} (cut={args.cut}s, PlayResY={play_res_y(args.height, args.band)})", flush=True)
    try:
        rc = subprocess.run(cmd, cwd=srt.resolve().parent).returncode
    except FileNotFoundError:
        print("burn_subtitles: không tìm thấy ffmpeg trên PATH — cài ffmpeg bản đầy đủ "
              "(Windows: winget install Gyan.FFmpeg, rồi mở terminal MỚI) và chạy lại", file=sys.stderr)
        return 2
    if rc != 0:
        print(f"burn_subtitles: ffmpeg thoát rc={rc}", file=sys.stderr)
        reason = explain_ffmpeg_failure()   # chỉ SAU khi ffmpeg lỗi — đường thành công không chạy thêm lệnh nào
        if reason:
            print(f"burn_subtitles: {reason}", file=sys.stderr)
        return rc
    sidecar = out.resolve().with_suffix(".srt")
    if sidecar.exists():
        print(f"burn_subtitles: CẢNH BÁO — {sidecar.name} nằm cạnh {out.name}; xoá/đổi tên trước khi giao "
              "(player sẽ vẽ thêm lớp phụ đề thứ hai)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    from text_io import force_utf8_stdio

    force_utf8_stdio()  # ngoài main(): test gọi main() với io.StringIO không có reconfigure
    sys.exit(main())
