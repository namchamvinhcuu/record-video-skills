#!/usr/bin/env python3
"""Every path to a script of this plugin, written in a document Claude reads, must start with
`${CLAUDE_PLUGIN_ROOT}/`.

An installed plugin lives in a cache folder the user never sees, so a bare `scripts/recorder.py` in SKILL.md
makes Claude look in the user's working folder and fail. The documents that carry commands (the skill, its
reference and every slash command) are checked here on every run, so a later edit cannot reintroduce a bare path.

Run: python tests/test_plugin_root_paths.py
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PREFIX = "${CLAUDE_PLUGIN_ROOT}/"
BARE = re.compile(r"(?<!\}/)(?<![A-Za-z0-9_./-])scripts/")


def documents() -> list[Path]:
    docs = sorted((ROOT / "skills").rglob("*.md")) + sorted((ROOT / "commands").glob("*.md"))
    return [p for p in docs if p.is_file()]


def bare_references(text: str) -> list[tuple[int, str]]:
    """(line number, line) of every `scripts/` that is not preceded by ${CLAUDE_PLUGIN_ROOT}/."""
    return [(n, line.strip()[:140]) for n, line in enumerate(text.splitlines(), 1) if BARE.search(line)]


class TestDocuments(unittest.TestCase):
    def test_the_documents_to_check_exist(self):
        names = {p.name for p in documents()}
        self.assertTrue({"SKILL.md", "REFERENCE.md", "record-demo.md", "check-env.md"} <= names, names)

    def test_no_bare_scripts_path_in_any_document(self):
        for path in documents():
            with self.subTest(document=path.relative_to(ROOT).as_posix()):
                bad = bare_references(path.read_text(encoding="utf-8"))
                self.assertEqual(bad, [], f"use {PREFIX}scripts/… instead of a bare path")

    def test_the_skill_mentions_the_scripts_through_the_plugin_root(self):
        text = (ROOT / "skills" / "record-demo-video" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(PREFIX + "scripts/", text)


class TestTheCheckerItself(unittest.TestCase):
    def test_a_bare_path_is_found(self):
        self.assertTrue(bare_references("run `python scripts/build_srt.py`"))
        self.assertTrue(bare_references("see (scripts/tests/test_x.py)"))
        self.assertTrue(bare_references("```\ncd scripts/\n```"))

    def test_a_prefixed_path_passes(self):
        self.assertEqual(bare_references("run `python ${CLAUDE_PLUGIN_ROOT}/scripts/build_srt.py`"), [])

    def test_other_folders_ending_in_scripts_are_not_this_folder(self):
        self.assertEqual(bare_references("the demo-scripts/ folder and my_scripts/x"), [])


if __name__ == "__main__":
    unittest.main()
