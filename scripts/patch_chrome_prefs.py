#!/usr/bin/env python3
"""patch_chrome_prefs.py — tắt Chrome Translate popup + bubble lưu mật khẩu bằng cách patch thẳng file
`Preferences` (JSON) trong profile, TRƯỚC KHI Chrome khởi động lần đầu với profile đó.

Vì sao không dùng CLI flag: `--disable-features=Translate` không chặn được popup ở mọi
phiên bản Chrome (hành vi đổi giữa version, không đáng tin — verify thực nghiệm).
Patch profile SẠCH (mới tạo, chưa chạy Chrome lần nào) là chắc ăn nhất — Chrome có thể
ghi đè lại một số key nếu profile đã từng chạy trước khi patch. Xem REFERENCE.md §5.

Chạy: python patch_chrome_prefs.py --profile-dir <user_data_dir> --lang vi
Import: from patch_chrome_prefs import patch_chrome_prefs
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# lang.upper() KHÔNG cho region code BCP-47 đúng ("vi".upper() = "VI" = US Virgin Islands,
# không phải Việt Nam; "ko".upper() = "KO" không phải mã ISO-3166-1 nào). Liệt kê đúng cho ngôn ngữ hay dùng; ngôn
# ngữ khác fallback về upper() — best-effort, không đảm bảo đúng chuẩn.
_LANG_REGION = {"vi": "VN", "ko": "KR", "en": "US"}


def patch_chrome_prefs(profile_dir: str, lang: str = "vi") -> Path:
    """Ghi/patch `<profile_dir>/Default/Preferences`. Trả về path đã ghi."""
    prefs_path = Path(profile_dir) / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True, exist_ok=True)

    prefs: dict = {}
    if prefs_path.exists():
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))

    prefs["translate_enabled"] = False
    # Tắt bubble "Lưu mật khẩu?" sau bước login — nó hiện USERNAME dạng chữ rõ ở góc
    # phải trên của video giao khách (đo Chrome 155 + Playwright 1.63, 2026-10-19).
    prefs["credentials_enable_service"] = False
    prefs.setdefault("profile", {})["password_manager_enabled"] = False
    intl = prefs.setdefault("intl", {})
    region = _LANG_REGION.get(lang, lang.upper())
    intl["accept_languages"] = f"{lang},{lang}-{region}"

    prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    return prefs_path


if __name__ == "__main__":
    from text_io import force_utf8_stdio

    force_utf8_stdio()  # TRƯỚC argparse: --help có tiếng Việt, đường dẫn profile Windows có thể có dấu
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile-dir", required=True, help="user_data_dir của Chrome profile (KHÔNG dùng profile thật)")
    ap.add_argument("--lang", default="vi", help="mã ngôn ngữ (vi/ko/en...), mặc định vi")
    args = ap.parse_args()
    written = patch_chrome_prefs(args.profile_dir, lang=args.lang)
    print(f"Patched: {written}")
