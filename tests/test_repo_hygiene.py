#!/usr/bin/env python3
"""Hygiene test for a PUBLIC repo: no secret, local path, internal address or private name anywhere.

What it scans: EVERYTHING the repo would publish — every tracked file and every untracked-but-not-ignored file
(`git ls-files -co --exclude-standard`), their file and directory NAMES, this test file — and, when a `.git`
exists, the whole commit HISTORY (message + patch of every commit on every ref, merges included, tag messages,
and the author/committer e-mail addresses). Without its own `.git` (a copied folder, the installed plugin cache)
it walks the tree instead.

Two layers of rules:
- GENERIC rules live in this file (paths, e-mail, IP addresses, unknown URL hosts, credential literals, token
  and key markers, wikilinks, tool-workflow slugs). They contain no name of any person or customer.
- PRIVATE terms (the owner's name, customer and project names, internal tooling words) are NOT in this file.
  They are read from `.git/info/hygiene-terms.txt` (inside `.git`, so never published) or from the file named by
  the environment variable RVS_HYGIENE_TERMS_FILE. When the file is absent the private part is skipped with a
  printed reason; HYGIENE_REQUIRE_PRIVATE_TERMS=1 turns that into a failure. File format, one per line:
  `term` (case-insensitive; a term of 4 letters or fewer must stand alone, a longer one also matches inside a
  longer word), `cs: Term` (case-sensitive), `re: <regex>`, `tooling: <file or folder name that must not be
  published>`, `author-ok: <e-mail allowed on commits>`, `author-name-ok: <a commit author name that is allowed
  although a rule matches it, e.g. the public repository owner's handle>`, `# comment`.

How it stays honest:
- text is NFKC-normalised and zero-width characters are removed before matching; every line is matched as written
  AND with CamelCase split into words;
- a file with NUL bytes that is not UTF-16 is reported as binary (nothing binary belongs in this repo);
- `__pycache__/` and `*.pyc` are skipped only when walking (git already ignores them); a symlink, or a nested
  repository, is a failure; the scan asserts it is not vacuous;
- the history scan SKIPS with a printed reason when there is no usable git or no commit, unless
  HYGIENE_REQUIRE_HISTORY=1, which turns anything that prevents the scan into a FAILURE; every commit author and
  committer e-mail must be a GitHub noreply address or listed as `author-ok`;
- exemptions: the exact repository slug (left/right guarded, also as a plain GitHub URL) and the name of the
  country in Vietnamese.

Run: python tests/test_repo_hygiene.py
Strict (before the first push): HYGIENE_REQUIRE_HISTORY=1 HYGIENE_REQUIRE_PRIVATE_TERMS=1 python tests/test_repo_hygiene.py
"""
from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_FILES = 20
REQUIRED_FILES = (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "scripts/recorder.py")
SLUG = "".join(["namcham", "vinhcuu", "/record-video-skills"])
EXEMPT_STRINGS = ("".join(["noreply@anthropic", ".com"]), "@x.test")     # trailer address; the test-only domain
NOREPLY_SUFFIX = "@users.noreply.github.com"
PRIVATE_TERMS_ENV = "RVS_HYGIENE_TERMS_FILE"
ALLOWED_URL_HOSTS = {"github.com", "www.w3.org", "localhost", "127.0.0.1", "demo.test", "your-odoo-instance",
                     "example.com", "example.org", "claude.com"}
TOOLING_NAMES = ["CLAUDE.md", "AGENTS.md", ".claude", ".gemini"]


def _t(*parts: str) -> str:
    return "".join(parts)


_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_COUNTRY = "Vi\u1ec7t " + _t("N", "am")
_SKIP_PARTS = {"__pycache__", ".git"}

# ---------------------------------------------------------------------------------------------------- generic
_BS = r"\\{1,2}"
GENERIC_RULES: list[tuple[str, "re.Pattern[str]"]] = [
    ("home path", re.compile("|".join([_t("/ho", "me/[A-Za-z]"), _t("/Us", "ers/[A-Za-z]"), _t("/Vol", "umes/"),
                                       _t("/ro", "ot/"), _t("/m", "nt/[A-Za-z]"),
                                       r"[A-Za-z]:" + _BS + _t("Us", "ers") + _BS]), re.IGNORECASE)),
    ("e-mail address", re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")),
    ("IPv4 address", re.compile(r"(?<![\d.])(?!127\.0\.0\.1(?![\d.]))(?!0\.0\.0\.0(?![\d.]))(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")),
    ("credential literal", re.compile(
        r"""(?i)(?:["'](?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|login|user(?:name)?)["']\s*:"""
        r"""|\b(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|login|user(?:name)?)\s*=)"""
        r"""\s*["'](?![$<{%*])(?!.*ODOO_DEMO_)[^"'\n]+["']""")),
    ("commit hash of another repo", re.compile(r"\bcommit [0-9a-f]{7,40}\b")),
    ("private key block", re.compile("-" * 5 + "BEGIN")),
    ("token literal", re.compile(r"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")),
    ("internal workflow id", re.compile(r"\brdv-(?!test-|video-|rec-|stealth-hang-|x11)[a-z0-9]+-[a-z0-9]+")),
]
_WIKILINK = re.compile(r"\[\[")                      # markdown only
_URL_HOST = re.compile(r"https?://([A-Za-z0-9._-]+)")


def _compile_term(term: str, case_sensitive: bool) -> "re.Pattern[str]":
    flags = 0 if case_sensitive else re.IGNORECASE
    parts = re.findall(r"[A-Za-z0-9]+", term)
    # a multi-word term matches with ANY separator between its words (space, _, ., -, none): `acme-corp` also
    # catches `acme corp`, `acme_corp` and `AcmeCorp`
    body = r"[\s_.\-]*".join(re.escape(p) for p in parts) if len(parts) > 1 else re.escape(term)
    if len("".join(parts)) <= 4:                     # short terms must stand alone: "xyz" is not "chxyzx"
        return re.compile(r"(?<![A-Za-z])" + body + r"(?![A-Za-z])", flags)
    return re.compile(body, flags)                   # long terms also match glued into a longer word


class Rules:
    """Generic rules + the private terms (labels never contain the term itself)."""

    def __init__(self, private_lines: list[str] | None = None):
        self.private: list[tuple[str, "re.Pattern[str]"]] = []
        self.allowed_authors: set[str] = set()
        self.allowed_names: set[str] = set()
        self.tooling = list(TOOLING_NAMES)
        for raw in private_lines or []:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            n = len(self.private) + 1
            if line.startswith("cs:"):
                self.private.append((f"private term #{n}", _compile_term(line[3:].strip(), True)))
            elif line.startswith("re:"):
                self.private.append((f"private pattern #{n}", re.compile(line[3:].strip(), re.IGNORECASE)))
            elif line.startswith("tooling:"):
                self.tooling.append(line[8:].strip())
            elif line.startswith("author-ok:"):
                self.allowed_authors.add(line[10:].strip().lower())
            elif line.startswith("author-name-ok:"):
                self.allowed_names.add(line[15:].strip().lower())
            else:
                self.private.append((f"private term #{n}", _compile_term(line, False)))

    @property
    def all(self) -> list[tuple[str, "re.Pattern[str]"]]:
        return GENERIC_RULES + self.private


def private_terms_path(root: Path = ROOT) -> Path:
    env = os.environ.get(PRIVATE_TERMS_ENV)
    return Path(env) if env else root / ".git" / "info" / "hygiene-terms.txt"


def load_rules(root: Path = ROOT) -> tuple[Rules, str | None]:
    """(rules, reason the private part is missing or None)."""
    path = private_terms_path(root)
    if not path.is_file():
        return Rules(), f"no private term list at {path.name} (generic rules only)"
    return Rules(path.read_text(encoding="utf-8").splitlines()), None


def _clean(text: str) -> str:
    """Normalise, then remove the exemptions: the exact slug (guarded, also as a GitHub URL), the country name."""
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    # replaced by ONE SPACE (not ""), so the text around an exemption can never glue into a forbidden word
    text = re.sub(r"(?:(?<![\w./-])|(?<=github\.com/))" + re.escape(SLUG) + r"(?![\w.-])", " ", text)
    text = re.sub(r"(?<![A-Za-z])" + re.escape(_COUNTRY) + r"(?![A-Za-z])", " ", text)
    for exempt in EXEMPT_STRINGS:
        text = text.replace(exempt, "")
    return text


def scan_text(text: str, *, markdown: bool = False, rules: Rules | None = None) -> list[str]:
    """`line N: <label>` for every rule that matches (each line is also matched with CamelCase split)."""
    rules = rules or Rules()
    found: list[str] = []
    for number, line in enumerate(_clean(text).splitlines(), 1):
        split = _CAMEL.sub(" ", line)
        variants = {line, split, _ACRONYM.sub(r"\1 \2", split)}      # `XyzBankTest` and `XYZBankTest` both split
        labels: list[str] = []
        for label, rule in rules.all:
            if any(rule.search(v) for v in variants) and label not in labels:
                labels.append(label)
        for host in _URL_HOST.findall(line):
            if host.lower() not in ALLOWED_URL_HOSTS and "unknown URL host" not in labels:
                labels.append("unknown URL host")
        if markdown and _WIKILINK.search(line):
            labels.append("wikilink")
        found += [f"line {number}: {label}" for label in labels]
    return found


def decode(data: bytes) -> tuple[str, bool]:
    """(text, is_binary). UTF-8 (BOM or not) and UTF-16 (BOM, or BOM-less LE/BE) are text; anything else with NULs
    is binary."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", errors="replace"), b"\x00" in data      # a BOM must not hide a NUL payload
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = data.decode("utf-16", errors="replace")
        return text, "\x00" in text
    head = data[:512]
    if b"\x00" not in head and b"\x00" not in data:
        return data.decode("utf-8", errors="replace"), False
    if len(data) >= 2 and head[0] == 0 and head[1] != 0:
        text = data.decode("utf-16-be", errors="replace")
        return text, "\x00" in text
    if len(data) >= 2 and head[0] != 0 and head[1] == 0:
        text = data.decode("utf-16-le", errors="replace")
        return text, "\x00" in text
    return data.decode("latin-1"), True


def list_files(root: Path) -> tuple[list[str], str]:
    """(relative POSIX paths, how they were listed). Tracked + untracked-not-ignored via git when `root` has its own
    `.git` (git already ignores caches); otherwise a walk that skips `__pycache__` and `*.pyc`."""
    paths: set[str] = set()
    how = "walk"
    if (root / ".git").exists():
        try:
            proc = subprocess.run(["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            paths = {p for p in proc.stdout.decode("utf-8", errors="replace").split("\0") if p}
            how = "git"
    if how == "walk":
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _SKIP_PARTS]
            for name in dirs + files:           # symlinked dirs show up in `dirs`: keep them so they are reported
                full = Path(base, name)
                if full.is_symlink() or full.is_file():
                    paths.add(full.relative_to(root).as_posix())
        paths = {p for p in paths if not p.endswith(".pyc")}
    return [p for p in sorted(paths) if os.path.lexists(root / p)], how


def scan_tree(root: Path, rules: Rules | None = None) -> list[str]:
    """Every problem in the publishable set of `root`, as `path: detail` strings."""
    rules = rules or Rules()
    problems: list[str] = []
    files, _ = list_files(root)
    for rel in files:
        for hit in scan_text(rel, rules=rules):
            problems.append(f"{rel}: file name — {hit.split(': ', 1)[1]}")
        full = root / rel
        if full.is_symlink():
            problems.append(f"{rel}: is a symlink (a plugin repo ships none)")
            continue
        if full.is_dir():
            problems.append(f"{rel}: is a directory entry (a nested repository?)")
            continue
        if not full.is_file():
            continue
        text, binary = decode(full.read_bytes())
        if binary:
            problems.append(f"{rel}: binary or unreadable content (NUL bytes) — nothing binary belongs in this repo")
        for hit in scan_text(text, markdown=rel.lower().endswith(".md"), rules=rules):
            problems.append(f"{rel}: {hit}")
    return problems


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=120)


def scan_history(repo: Path, rules: Rules | None = None) -> tuple[list[str], str | None]:
    """(problems, skip_reason). Scans message + patch (merges included) of every commit on every ref, tag messages and
    the author/committer e-mail addresses. When the scan cannot run the reason is returned; the caller decides
    (HYGIENE_REQUIRE_HISTORY=1 turns it into a failure)."""
    rules = rules or Rules()
    if not (repo / ".git").exists():
        return [], "no .git directory"
    try:
        log = _git(repo, "log", "-p", "-m", "--all", "--no-ext-diff", "--no-textconv", "--format=%B")
        count = _git(repo, "rev-list", "--all", "--count")
        authors = _git(repo, "log", "--all", "--format=%ae%n%ce")
        names = _git(repo, "log", "--all", "--format=%an%n%cn")
        # annotated tags: name, tagger name/e-mail and message; every ref (branches too): its name
        tags = _git(repo, "for-each-ref", "--format=%(refname)%0a%(taggername)%0a%(taggeremail)%0a%(contents)", "refs/tags")
        refs = _git(repo, "for-each-ref", "--format=%(refname)")
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"git could not run ({type(exc).__name__})"
    if log.returncode != 0:
        return [], "git log failed: " + log.stderr.decode("utf-8", errors="replace").strip()[:120]
    if count.returncode != 0 or int(count.stdout.decode().strip() or "0") < 1:
        return [], "the repository has no commit yet"
    problems: list[str] = []
    tag_text = tags.stdout.decode("utf-8", errors="replace")
    patch_text = log.stdout.decode("utf-8", errors="replace")
    distinct_names = {n.strip() for n in names.stdout.decode("utf-8", errors="replace").splitlines() if n.strip()}
    name_text = "\n".join(sorted(n for n in distinct_names if n.lower() not in rules.allowed_names))
    # Patch lines carry a one-character diff prefix (+, -, space): drop it so `+@decorator` is not read as an e-mail
    # local part. The wikilink rule applies only to the hunks of markdown files (Python/JS nested lists are fine).
    segments: list[tuple[bool, list[str]]] = [(False, [])]
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            segments.append((line.lower().rstrip().endswith(".md"), [line]))
            continue
        segments[-1][1].append(line[1:] if line[:1] in ("+", "-", " ") else line)
    for markdown, lines in segments:
        for hit in scan_text("\n".join(lines), markdown=markdown, rules=rules):
            problems.append(f"history: {hit}")
    for hit in scan_text(tag_text + "\n" + refs.stdout.decode("utf-8", errors="replace") + "\n" + name_text, rules=rules):
        problems.append(f"history: {hit}")
    if re.search(r"^(GIT binary patch|Binary files .* differ)$", patch_text, re.MULTILINE):
        problems.append("history: a commit holds a binary file (nothing binary belongs in this repo)")
    allowed = rules.allowed_authors
    found_emails = {e.strip() for e in authors.stdout.decode("utf-8", errors="replace").split() if e.strip()}
    found_emails |= set(re.findall(r"(?<=<)[^<>\s]+@[^<>\s]+(?=>)", tag_text))     # annotated-tag taggers
    for email in sorted(found_emails):
        if not (email.lower().endswith(NOREPLY_SUFFIX) or email.lower() in allowed):
            problems.append("history: a commit author/committer e-mail is neither a GitHub noreply address nor "
                            f"`author-ok` (set repo-local user.email; found {email.split('@')[-1]!r} domain)")
    return problems, None


# ---------------------------------------------------------------------------------------------------- the repo
class TestTree(unittest.TestCase):
    rules: Rules
    private_missing: str | None

    @classmethod
    def setUpClass(cls):
        cls.rules, cls.private_missing = load_rules()

    def test_the_tree_is_not_vacuous(self):
        files, how = list_files(ROOT)
        self.assertGreaterEqual(len(files), MIN_FILES, f"only {len(files)} files listed via {how}")
        for needed in REQUIRED_FILES:
            self.assertIn(needed, files)

    def test_the_private_term_list_is_present_when_required(self):
        if self.private_missing:
            print(f"PRIVATE TERMS: {self.private_missing}", file=sys.stderr)
            if os.environ.get("HYGIENE_REQUIRE_PRIVATE_TERMS") == "1":
                self.fail(f"HYGIENE_REQUIRE_PRIVATE_TERMS=1 but {self.private_missing}")
            self.skipTest(self.private_missing)
        self.assertTrue(self.rules.private, "the private term file is empty")

    def test_no_forbidden_term_in_any_file_or_file_name(self):
        problems = scan_tree(ROOT, self.rules)
        self.assertEqual(problems, [], "\n" + "\n".join(problems[:60]))

    def test_assistant_tooling_files_are_not_in_the_publishable_set(self):
        files, _ = list_files(ROOT)
        for bad in self.rules.tooling:
            self.assertFalse([f for f in files if f == bad or f.startswith(bad + "/")], bad)

    def test_the_tracked_gitignore_holds_only_plugin_patterns(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for tool_dir in self.rules.tooling[2:]:
            self.assertNotIn(tool_dir, text, "tooling patterns belong in .git/info/exclude, not the public .gitignore")


class TestHistory(unittest.TestCase):
    def test_no_forbidden_term_in_the_history(self):
        strict = os.environ.get("HYGIENE_REQUIRE_HISTORY") == "1"
        rules, _ = load_rules()
        problems, skipped = scan_history(ROOT, rules)
        if skipped:
            print(f"HISTORY SCAN SKIPPED: {skipped}", file=sys.stderr)
            if strict:
                self.fail(f"HYGIENE_REQUIRE_HISTORY=1 but the history scan could not run: {skipped}")
            self.skipTest(skipped)
        self.assertEqual(problems, [], "\n" + "\n".join(problems[:60]))


# ---------------------------------------------------------------------------------------------------------
# The scanner itself: each case builds its OWN temp tree/repo; the private terms here are FAKE.

FAKE = Rules(["acmecorp", "cs: Zed", "xyz", r"re: bob\s+builder", "tooling: .fake-tool", "author-ok: dev@x.test"])
BAD_WORD = "acmecorp"


def make_tree(files: dict[str, bytes | str]) -> Path:
    root = Path(tempfile.mkdtemp(prefix="rvs-hygiene-"))
    base: dict[str, bytes | str] = {p: "ok\n" for p in REQUIRED_FILES}
    base.update({f"scripts/f{i}.py": "x = 1\n" for i in range(MIN_FILES)})
    base.update(files)
    for rel, data in base.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return root


class TestScannerRules(unittest.TestCase):
    def hits(self, text, **kw):
        return scan_text(text, rules=kw.pop("rules", FAKE), **kw)

    def test_private_terms_are_found_in_every_spelling(self):
        for text in (f"from {BAD_WORD} fixtures", "AcmeCorpBankTest", f"glued_{BAD_WORD}_here", "ACMECORP",
                     "a\u200bcmecorp", "\uff41\uff43\uff4d\uff45\uff43\uff4f\uff52\uff50"):   # zero-width, full-width
            with self.subTest(text=text):
                self.assertTrue(self.hits(text + "\n"), text)

    def test_a_short_term_stands_alone_or_as_a_camel_case_word(self):
        self.assertTrue(self.hits("the xyz menu\n"))
        self.assertTrue(self.hits("XyzMenuTest\n"))
        self.assertEqual(self.hits("a chxyzx word\n"), [])

    def test_a_multi_word_term_matches_with_any_separator(self):
        rules = Rules(["acme-corp"])
        for text in ("acme-corp", "acme corp", "acme  corp", "acme_corp", "AcmeCorp", "acmecorp", "ACME.CORP", "acme\tcorp"):
            with self.subTest(text=text):
                self.assertTrue(self.hits(text + "\n", rules=rules), text)
        self.assertEqual(self.hits("acme and corp\n", rules=rules), [])

    def test_a_short_term_is_caught_in_acronym_glue(self):
        for text in ("XYZBankTest", "XyzBankTest", "XYZ Bank"):
            with self.subTest(text=text):
                self.assertTrue(self.hits(text + "\n"), text)

    def test_case_sensitive_and_regex_terms(self):
        self.assertTrue(self.hits("Zed said\n"))
        self.assertEqual(self.hits("zed said\n"), [])
        self.assertTrue(self.hits("Bob   Builder\n"))

    def test_the_exact_slug_passes_but_a_longer_or_other_owner_path_does_not(self):
        self.assertEqual(self.hits(f"claude plugin marketplace add {SLUG}\n"), [])
        self.assertEqual(self.hits(f"({SLUG})\n"), [])
        self.assertEqual(self.hits(f"https://github.com/{SLUG}\n"), [], "the plain GitHub URL of the repo")
        owner = Rules([SLUG.split("/")[0]])
        self.assertTrue(self.hits(f"https://github.com/other/{SLUG}\n", rules=owner), "a path under another owner")
        self.assertTrue(self.hits(f"{SLUG}-fork\n", rules=owner), "a longer slug is not exempt")
        self.assertEqual(self.hits(f"{SLUG}\n", rules=owner), [], "the exact slug alone is exempt")

    def test_the_country_name_passes_when_standing_alone_only(self):
        first_name = _t("n", "am")
        owner = Rules([first_name])
        self.assertEqual(self.hits(f"ISO: {_COUNTRY}\n", rules=owner), [])
        self.assertTrue(self.hits(f"x{_COUNTRY}y {first_name}\n", rules=owner))
        self.assertTrue(self.hits("Mr " + first_name.upper() + " here\n", rules=owner))
        self.assertEqual(self.hits("the name of the file; xyzzy\n", rules=Rules(["xyz"])), [])

    def test_wikilinks_only_in_markdown(self):
        self.assertTrue(self.hits("see [[Some-Note]]\n", markdown=True))
        self.assertEqual(self.hits("rows = [[1, 2], [3, 4]]\n"), [])

    def test_credential_literals_in_every_common_shape(self):
        word = "pass" + "word"
        for key in (word, "login", "token", "api_key", "user" + "name", "pwd", "secret"):
            with self.subTest(key=key):
                self.assertTrue(self.hits(f'{key} = "hunter2x"\n'), key)
                self.assertTrue(self.hits(f"{key} = 'k'\n"), key)
                self.assertTrue(self.hits(f'{{"{key}": "hunter2x"}}\n'), key)
        for text in (f'{word} = os.environ["ODOO_DEMO_{word.upper()}"]', f'{word} = "${{ODOO_PW}}"', f'{word} = "<your {word}>"',
                     'the token: "MARK 2.7 open login" would be skipped'):
            with self.subTest(clean=text):
                self.assertEqual(self.hits(text + "\n"), [], text)

    def test_paths_emails_ips_and_key_markers(self):
        for text in (_t("/ho", "me/someone/p"), _t("/ro", "ot/.ssh"), _t("/m", "nt/data"), _t("C:\\", "Users\\bob"),
                     _t("C:\\\\", "Users\\\\bob"), "mail a.b" + "@example.org", "host " + _t("192.", "168.1.50"),
                     _t("ht", "tp://10.", "0.5.7:8069"), "-" * 5 + "BEGIN RSA PRIVATE KEY", "see commit " + "a" * 7 + "b"):
            with self.subTest(text=text):
                self.assertTrue(self.hits(text + "\n"), text)
        for text in ("listen on 127.0.0.1", "bind 0.0.0.0", "version 1.62.0", "x = 1.2.3.4.5"):
            with self.subTest(clean=text):
                self.assertEqual([h for h in self.hits(text + "\n") if "IPv4" in h], [], text)

    def test_unknown_url_hosts_are_found_and_the_allow_list_passes(self):
        self.assertTrue(self.hits("see " + _t("ht", "tps://internal.corp.example.net/x") + "\n"))
        self.assertTrue(self.hits("see " + _t("ht", "tp://odoo.lan:8069/") + "\n"))
        self.assertEqual(self.hits("see https://github.com/x and http://www.w3.org/2000/svg\n"), [])

    def test_internal_workflow_ids_are_found_but_the_temp_prefixes_pass(self):
        self.assertTrue(self.hits("Plan " + _t("rdv-", "stealth-window-size") + "\n"))
        for ok in ('prefix="rdv-video-"', 'prefix="rdv-test-recorder-"', 'prefix="rdv-stealth-hang-"', 'prefix="rdv-rec-"'):
            self.assertEqual(self.hits(ok + "\n"), [], ok)


class TestScannerTree(unittest.TestCase):
    def setUp(self):
        self.dirs: list[Path] = []
        self.addCleanup(lambda: [shutil.rmtree(d, ignore_errors=True) for d in self.dirs])

    def tree(self, files):
        root = make_tree(files)
        self.dirs.append(root)
        return root

    def test_a_clean_tree_passes(self):
        self.assertEqual(scan_tree(self.tree({}), FAKE), [])

    def test_a_term_in_a_test_file_a_file_name_and_a_directory_name_is_found(self):
        for files, what in (({"tests/test_x.py": f"# from {BAD_WORD}\n"}, "tests/test_x.py"),
                            ({f"scripts/{BAD_WORD}_notes.txt": "fine\n"}, "file name"),
                            ({f"scripts/{BAD_WORD}/readme.txt": "fine\n"}, "file name")):
            with self.subTest(what=what):
                self.assertTrue(any(what in p for p in scan_tree(self.tree(files), FAKE)))

    def test_a_symlink_is_a_failure(self):
        root = self.tree({})
        try:
            os.symlink(root / "scripts" / "f0.py", root / "scripts" / "link.py")
        except (OSError, NotImplementedError):
            self.skipTest("this machine cannot create symlinks")
        self.assertTrue(any("symlink" in p for p in scan_tree(root, FAKE)))

    def test_pycache_and_pyc_are_skipped_when_walking(self):
        root = self.tree({"scripts/__pycache__/x.cpython-312.pyc": BAD_WORD.encode(), "scripts/y.pyc": BAD_WORD.encode()})
        self.assertEqual(scan_tree(root, FAKE), [])

    def test_utf16_text_is_decoded_and_scanned_in_every_flavour(self):
        text = f"{BAD_WORD}\n"
        for name, data in (("le-bom", codecs.BOM_UTF16_LE + text.encode("utf-16-le")),
                           ("be-bom", codecs.BOM_UTF16_BE + text.encode("utf-16-be")),
                           ("le", text.encode("utf-16-le")), ("be", text.encode("utf-16-be"))):
            with self.subTest(flavour=name):
                root = self.tree({"scripts/u.txt": data})
                self.assertTrue(any("private term" in p for p in scan_tree(root, FAKE)), name)

    def test_a_utf8_bom_cannot_hide_a_binary_payload(self):
        root = self.tree({"scripts/b.txt": b"\xef\xbb\xbf" + f"{BAD_WORD}\n".encode("utf-16-le")})
        self.assertTrue(any("binary" in p for p in scan_tree(root, FAKE)))

    def test_binary_content_is_a_failure(self):
        for name, data in (("utf-32", text_utf32()), ("png-like", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"), ("nul", b"abc\x00def")):
            with self.subTest(kind=name):
                root = self.tree({"scripts/b.bin": data})
                self.assertTrue(any("binary" in p for p in scan_tree(root, FAKE)), name)

    def test_a_tooling_name_in_the_set_is_listed_as_a_problem_name(self):
        self.assertIn(".fake-tool", FAKE.tooling)
        self.assertIn("CLAUDE.md", FAKE.tooling)


def text_utf32() -> bytes:
    return f"{BAD_WORD}\n".encode("utf-32-le")


def git(repo: Path, *args: str, email: str = "t@x.test") -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", f"user.email={email}", *args],
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestScannerHistory(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="rvs-hygiene-git-"))
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        git(self.repo, "init", "-q", "-b", "main")
        self.rules = Rules(["acmecorp", "author-ok: t@x.test"])

    def commit(self, name: str, data: str | bytes, message: str, email: str = "t@x.test"):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        git(self.repo, "add", "-A", email=email)
        git(self.repo, "commit", "-q", "-m", message, email=email)

    def test_a_term_added_in_one_commit_and_deleted_in_the_next_is_found_in_history_only(self):
        self.commit("a.txt", f"{BAD_WORD}\n", "add a file")
        self.commit("a.txt", "clean\n", "clean it up")
        self.assertEqual(scan_tree(self.repo, self.rules), [], "the tree is clean")
        problems, skipped = scan_history(self.repo, self.rules)
        self.assertIsNone(skipped)
        self.assertTrue([p for p in problems if "private term" in p], problems)

    def test_a_term_in_a_commit_message_and_in_a_tag_message_is_found(self):
        self.commit("a.txt", "clean\n", f"notes from {BAD_WORD}")
        self.assertTrue(scan_history(self.repo, self.rules)[0])
        other = Path(tempfile.mkdtemp(prefix="rvs-hygiene-tag-"))
        self.addCleanup(lambda: shutil.rmtree(other, ignore_errors=True))
        git(other, "init", "-q", "-b", "main")
        (other / "a.txt").write_text("clean\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "plain")
        git(other, "tag", "-a", "v1", "-m", f"release by {BAD_WORD}")
        self.assertTrue([p for p in scan_history(other, self.rules)[0] if "private term" in p])

    def test_a_term_hidden_in_a_merge_commit_is_found(self):
        self.commit("a.txt", "base\n", "base")
        git(self.repo, "checkout", "-q", "-b", "side")
        self.commit("b.txt", "side\n", "side work")
        git(self.repo, "checkout", "-q", "main")
        self.commit("c.txt", "main\n", "main work")
        git(self.repo, "merge", "-q", "--no-ff", "--no-commit", "side")
        (self.repo / "a.txt").write_text(f"base {BAD_WORD}\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "merge")
        self.assertTrue([p for p in scan_history(self.repo, self.rules)[0] if "private term" in p])

    def test_a_binary_blob_in_history_is_a_problem(self):
        self.commit("a.bin", b"\x00\x01\x02binary", "add blob")
        self.assertTrue([p for p in scan_history(self.repo, self.rules)[0] if "binary" in p])

    def test_author_and_committer_e_mail_must_be_noreply_or_allow_listed(self):
        self.commit("a.txt", "clean\n", "plain", email="private.person@" + "gmail.com")
        problems = scan_history(self.repo, self.rules)[0]
        self.assertTrue([p for p in problems if "e-mail" in p], problems)
        self.assertNotIn("private.person", " ".join(problems), "the address itself must not be echoed")

    def test_an_author_name_a_branch_name_and_a_tagger_are_scanned(self):
        # author NAME
        git(self.repo, "config", "user.name", f"{BAD_WORD} person")
        (self.repo / "a.txt").write_text("clean\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "-C", str(self.repo), "-c", f"user.name={BAD_WORD} person", "-c", "user.email=t@x.test",
                        "commit", "-q", "-m", "plain"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue([p for p in scan_history(self.repo, self.rules)[0] if "private term" in p], "author name")
        # branch name
        other = Path(tempfile.mkdtemp(prefix="rvs-hygiene-ref-"))
        self.addCleanup(lambda: shutil.rmtree(other, ignore_errors=True))
        git(other, "init", "-q", "-b", "main")
        (other / "a.txt").write_text("clean\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "plain")
        git(other, "branch", f"feature-{BAD_WORD}")
        self.assertTrue([p for p in scan_history(other, self.rules)[0] if "private term" in p], "branch name")
        # tagger e-mail
        git(other, "branch", "-D", f"feature-{BAD_WORD}")
        git(other, "tag", "-a", "v1", "-m", "release", email="someone@" + "gmail.com")
        # the allow-list verdict specifically ("neither a GitHub noreply address"), not the generic e-mail text rule
        self.assertTrue([p for p in scan_history(other, self.rules)[0] if "noreply" in p], "tagger e-mail")

    def test_noreply_and_allow_listed_authors_pass_and_the_slug_and_country_pass(self):
        self.commit("a.txt", f"install: {SLUG}\nin {_COUNTRY}\n", "plain", email="123+someone" + NOREPLY_SUFFIX)
        self.commit("b.txt", "x\n", "plain two")        # author-ok t@x.test
        self.assertEqual(scan_history(self.repo, self.rules), ([], None))

    def test_a_clean_realistic_history_passes(self):
        """The false positives a real first commit showed: nested lists, a decorator line, the words of a binary-patch
        check in source, and an author name that a private rule matches but is allow-listed."""
        rules = Rules(["acmecorp", "author-name-ok: acmecorp-bot"])
        code = "\n".join(["import unittest", "", "@unittest.skipUnless(True, 'x')", "class T(unittest.TestCase):",
                          "    pass", "rows = [[1, 2], [3, 4]]", 'probe = "GIT binary patch" in text', ""])
        (self.repo / "a.py").write_text(code)
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=acmecorp-bot", "-c", "user.email=t@x.test",
                        "commit", "-q", "-m", "plain"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        rules.allowed_authors.add("t@x.test")
        self.assertEqual(scan_history(self.repo, rules), ([], None))

    def test_a_co_author_trailer_in_a_commit_message_is_not_a_tagger_address(self):
        trailer = "Co-Authored-By: Some Assistant <" + "".join(["noreply@anthropic", ".com"]) + ">"
        self.commit("a.txt", "clean\n", "plain subject\n\n" + trailer)
        self.assertEqual(scan_history(self.repo, self.rules), ([], None))

    def test_a_wikilink_added_in_a_markdown_file_is_still_found_but_not_in_code(self):
        self.commit("n.md", "see [[Some-Note]]\n", "note")
        self.assertTrue([p for p in scan_history(self.repo, self.rules)[0] if "wikilink" in p])

    def test_an_author_name_that_is_not_allow_listed_is_still_found(self):
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        (self.repo / "a.txt").write_text("x\n")
        git(self.repo, "add", "-A")
        subprocess.run(["git", "-C", str(self.repo), "-c", f"user.name={BAD_WORD}", "-c", "user.email=t@x.test",
                        "commit", "-q", "-m", "plain"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue([p for p in scan_history(self.repo, self.rules)[0] if "private term" in p])

    def test_zero_commits_is_a_skip_with_a_reason(self):
        problems, skipped = scan_history(self.repo, self.rules)
        self.assertEqual(problems, [])
        self.assertIn("no commit", skipped or "")

    def test_no_git_directory_is_a_skip_with_a_reason(self):
        bare = Path(tempfile.mkdtemp(prefix="rvs-hygiene-nogit-"))
        self.addCleanup(lambda: shutil.rmtree(bare, ignore_errors=True))
        self.assertIn("no .git", scan_history(bare)[1] or "")

    def test_a_copied_folder_without_git_is_walked_not_skipped(self):
        root = make_tree({"tests/test_x.py": f"# {BAD_WORD}\n"})
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.assertEqual(list_files(root)[1], "walk")
        self.assertTrue(scan_tree(root, FAKE))

    def test_a_tracked_file_under_pycache_is_still_scanned_in_a_git_tree(self):
        self.commit("pkg/__pycache__/leak.txt", f"{BAD_WORD}\n", "force a cache file")
        files, how = list_files(self.repo)
        self.assertEqual(how, "git")
        self.assertIn("pkg/__pycache__/leak.txt", files)
        self.assertTrue(scan_tree(self.repo, self.rules))


class TestPrivateTermFile(unittest.TestCase):
    def test_the_loader_reads_every_line_kind_and_ignores_comments(self):
        rules = Rules(["# comment", "", "acmecorp", "cs: Zed", r"re: a\s+b", "tooling: .x", "author-ok: A@x.test"])
        self.assertEqual(len(rules.private), 3)
        self.assertIn(".x", rules.tooling)
        self.assertEqual(rules.allowed_authors, {"a@x.test"})

    def test_labels_never_contain_the_term(self):
        for label, _ in Rules(["acmecorp", "cs: Zed", r"re: bob"]).private:
            self.assertNotIn("acmecorp", label.lower())

    def test_a_missing_file_is_reported_not_raised(self):
        old = os.environ.get(PRIVATE_TERMS_ENV)
        os.environ[PRIVATE_TERMS_ENV] = str(Path(tempfile.gettempdir()) / "rvs-no-such-terms-file.txt")
        try:
            rules, reason = load_rules()
        finally:
            if old is None:
                os.environ.pop(PRIVATE_TERMS_ENV, None)
            else:
                os.environ[PRIVATE_TERMS_ENV] = old
        self.assertEqual(rules.private, [])
        self.assertIn("generic rules only", reason or "")


if __name__ == "__main__":
    unittest.main()
