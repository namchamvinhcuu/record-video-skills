#!/usr/bin/env python3
"""Test patch_chrome_prefs.py — I/O thật nhưng cô lập trong thư mục tạm của chính test
(không đụng profile Chrome thật nào). Chạy: python3 <file này>
"""
from __future__ import annotations
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "patch_chrome_prefs.py"

spec = importlib.util.spec_from_file_location("patch_chrome_prefs_mod", SCRIPT)
assert spec and spec.loader
pcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pcp)


class TestPatchChromePrefs(unittest.TestCase):
    def test_creates_preferences_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pcp.patch_chrome_prefs(tmp, lang="vi")
            self.assertTrue(path.exists())
            prefs = json.loads(path.read_text())
            self.assertEqual(prefs["translate_enabled"], False)
            self.assertEqual(prefs["intl"]["accept_languages"], "vi,vi-VN")

    def test_preserves_existing_unrelated_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            default_dir = Path(tmp) / "Default"
            default_dir.mkdir(parents=True)
            (default_dir / "Preferences").write_text(json.dumps({"some_other_key": 42}))
            path = pcp.patch_chrome_prefs(tmp, lang="ko")
            prefs = json.loads(path.read_text())
            self.assertEqual(prefs["some_other_key"], 42, "key không liên quan phải giữ nguyên")
            self.assertEqual(prefs["translate_enabled"], False)
            self.assertEqual(prefs["intl"]["accept_languages"], "ko,ko-KR")

    def test_disables_password_manager_bubble(self):
        # The "Save password?" bubble showed the demo username in clear text after login.
        with tempfile.TemporaryDirectory() as tmp:
            prefs = json.loads(pcp.patch_chrome_prefs(tmp, lang="vi").read_text())
            self.assertIs(prefs["credentials_enable_service"], False)
            self.assertIs(prefs["profile"]["password_manager_enabled"], False)

    def test_password_keys_merge_into_existing_profile_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            default_dir = Path(tmp) / "Default"
            default_dir.mkdir(parents=True)
            (default_dir / "Preferences").write_text(json.dumps({
                "credentials_enable_service": True,
                "profile": {"name": "Demo", "password_manager_enabled": True, "exit_type": "Normal"},
                "intl": {"selected_languages": "en"},
            }))
            prefs = json.loads(pcp.patch_chrome_prefs(tmp, lang="vi").read_text())
            self.assertIs(prefs["credentials_enable_service"], False)
            self.assertEqual(prefs["profile"], {"name": "Demo", "password_manager_enabled": False,
                                                "exit_type": "Normal"})
            self.assertEqual(prefs["intl"], {"selected_languages": "en", "accept_languages": "vi,vi-VN"})

    def test_lang_reflected_in_accept_languages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pcp.patch_chrome_prefs(tmp, lang="en")
            prefs = json.loads(path.read_text())
            self.assertEqual(prefs["intl"]["accept_languages"], "en,en-US")

    def test_unlisted_lang_falls_back_to_uppercase_region(self):
        # Ngôn ngữ chưa liệt kê trong _LANG_REGION — fallback upper() KHÔNG đảm bảo đúng
        # BCP-47 (đúng ra "ja" phải là "ja-JP", fallback cho "ja-JA" sai — best-effort có
        # giới hạn, ghi lại đúng hành vi thật thay vì giả vờ đã đúng chuẩn cho mọi ngôn ngữ).
        with tempfile.TemporaryDirectory() as tmp:
            path = pcp.patch_chrome_prefs(tmp, lang="ja")
            prefs = json.loads(path.read_text())
            self.assertEqual(prefs["intl"]["accept_languages"], "ja,ja-JA")


if __name__ == "__main__":
    unittest.main()
