#!/usr/bin/env python3
"""build_srt.py — parse log `MARK <elapsed_seconds> <step_name>` (in ra lúc chạy demo)
+ file narration JSON `{step_name: {"vi": "...", "ko": "..."}}` → sinh file `.srt`.

Logic tách PURE (parse_marks / build_cues / format_srt) khỏi I/O để test không cần file
thật. Xem REFERENCE.md §7 cho lý do OFFSET và vì sao cue kết thúc ở mark kế tiếp.

Chạy: python build_srt.py --log run.log --narration narration.json --lang vi --offset -0.3 --out out_vi.srt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass

# Tìm MARK ở BẤT KỲ đâu trong dòng (sau prefix logger `… INFO MARK 3.00 x`, hai MARK một dòng). Dòng tiến độ
# `\r` của ffmpeg trong log gộp làm `grep ^MARK` của shell bỏ sót — parser cũ thì vẫn
# đọc được vì splitlines() tách cả `\r`; nguyên nhân gốc đã sửa ở recorder.py (ffmpeg ghi log riêng).
_MARK_RE = re.compile(r"(?<![\w.])MARK[ \t]+(\d+(?:\.\d+)?)[ \t]+(\S+)")

# Cue cuối cùng không có "mark kế tiếp" để làm mốc kết thúc — giữ hiển thị ít nhất
# khoảng này (dài hơn nếu câu dài, xem cue_seconds) trước khi video/phụ đề kết thúc.
LAST_CUE_DURATION = 3.0
# Tốc độ đọc phụ đề (ký tự/giây) — cue ngắn hơn len/READ_CPS thì người xem không đọc kịp.
READ_CPS = 16.0


def cue_seconds(text: str, min_cue: float = LAST_CUE_DURATION, cps: float = READ_CPS) -> float:
    """Thời lượng tối thiểu để đọc kịp một câu phụ đề: max(min_cue, số ký tự / cps)."""
    return max(min_cue, len(text) / cps)


@dataclass(frozen=True)
class Mark:
    elapsed: float
    step: str


@dataclass(frozen=True)
class Cue:
    index: int
    start: float
    end: float
    text: str


def parse_marks(log_text: str) -> list[Mark]:
    """Đọc mọi `MARK <elapsed> <step>` theo thứ tự xuất hiện, bỏ qua phần còn lại (log ffmpeg/Playwright
    xen lẫn, kể cả dính cùng dòng qua `\\r`)."""
    return [Mark(elapsed=float(m.group(1)), step=m.group(2)) for m in _MARK_RE.finditer(log_text)]


def build_cues(marks: list[Mark], narration: dict[str, dict[str, str]],
                lang: str, offset: float = 0.0) -> list[Cue]:
    """Mỗi cue chạy từ mark[i]+offset tới mark[i+1]+offset; cue cuối kéo dài cue_seconds(text)
    (≥ LAST_CUE_DURATION) vì không có mốc kế tiếp làm điểm dừng. Bỏ qua step không có
    narration cho `lang` (không raise — cho phép narration chưa dịch hết mọi bước)."""
    cues: list[Cue] = []
    for i, mark in enumerate(marks):
        text = narration.get(mark.step, {}).get(lang)
        if not text:
            continue
        start = max(0.0, mark.elapsed + offset)
        if i + 1 < len(marks):
            end = max(start + 0.1, marks[i + 1].elapsed + offset)
        else:
            end = start + cue_seconds(text)
        cues.append(Cue(index=len(cues) + 1, start=start, end=end, text=text))
    return cues


def _srt_timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_srt(cues: list[Cue]) -> str:
    blocks = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n{_srt_timestamp(cue.start)} --> {_srt_timestamp(cue.end)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


if __name__ == "__main__":
    from text_io import force_utf8_stdio, read_text_sniffed

    force_utf8_stdio()  # TRƯỚC argparse: --help và dòng tổng kết có tiếng Việt/Hàn, console Windows mặc định cp1252/cp949
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", required=True, help="file log chứa dòng MARK <elapsed> <step>")
    ap.add_argument("--narration", required=True, help="JSON {step: {lang: text}}")
    ap.add_argument("--lang", required=True, help="mã ngôn ngữ cần xuất (vi/ko/en...)")
    ap.add_argument("--offset", type=float, default=0.0,
                     help="giây cộng vào mọi mốc thời gian (thường âm nhẹ — narration nên tới trước hành động)")
    ap.add_argument("--out", required=True, help="đường dẫn .srt xuất ra")
    args = ap.parse_args()

    # Windows: log do PowerShell 5.1 chuyển hướng là UTF-16, narration.json lưu từ Notepad có thể có BOM → đọc có dò BOM.
    log_text = read_text_sniffed(args.log)   # lỏng: chỉ các dòng MARK (ASCII) cần đọc đúng
    try:
        # chặt: lời thuyết minh đi thẳng vào phụ đề của video — ký tự hỏng KHÔNG được lặng lẽ thành U+FFFD
        narration = json.loads(read_text_sniffed(args.narration, strict=True))
    except UnicodeDecodeError as exc:
        print(f"build_srt: {args.narration} không phải UTF-8 hợp lệ ({exc.reason} ở byte {exc.start}) — "
              "lưu lại file narration với mã hoá UTF-8 rồi chạy lại", file=sys.stderr)
        sys.exit(2)

    marks = parse_marks(log_text)
    cues = build_cues(marks, narration, args.lang, offset=args.offset)
    # newline="\n": text mode trên Windows sẽ đổi \n thành \r\n trong file .srt.
    with open(args.out, "w", encoding="utf-8", newline="\n") as srt_file:
        srt_file.write(format_srt(cues))
    print(f"Đã ghi {len(cues)} cue vào {args.out} (từ {len(marks)} mark, lang={args.lang})")
