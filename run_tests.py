#!/usr/bin/env python3
"""Per-file test runner (stdlib only, works on Linux, macOS and Windows).

Every test file runs as its own script (``python <file>``): two of the files
drive a real browser, so ``unittest discover`` over the whole tree is not safe.

Contract
- ``--root <dir>``: the tree to test (default: the folder of this file), so the
  SAME runner can baseline another copy of the scripts.
- Test files: ``scripts/tests/test_*.py`` and ``tests/test_*.py`` under the root.
- A file is green at exit code 0, red otherwise (a timeout is red).
- A file that prints a line starting with ``SKIP-FILE: `` and exits 0 is a skip.
- Children get no stdin, ``PYTHONIOENCODING=utf-8`` and their output is decoded
  as UTF-8 with replacement, so a non-UTF-8 byte never crashes the runner.
- Exit code 0 only when no file is red; the LAST line is the grand total.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

SKIP_RE = re.compile(r"^SKIP-FILE: ", re.MULTILINE)
RAN_RE = re.compile(r"^Ran (\d+) tests?\b", re.MULTILINE)
TEST_GLOBS = ("scripts/tests/test_*.py", "tests/test_*.py")
DEFAULT_TIMEOUT = 900


def reconfigure_stdio():
    """Make our own output UTF-8 whatever the console code page is."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def find_tests(root, only=()):
    """Sorted test files under root; ``only`` restricts to those paths."""
    found = set()
    for pattern in TEST_GLOBS:
        found.update(p for p in root.glob(pattern) if p.is_file())
    if only:
        wanted = {(root / name).resolve() for name in only}
        found = {p for p in found if p.resolve() in wanted}
    return sorted(found)


def run_file(path, root, timeout):
    """Run one test file; return (status, tests_run, output) with status in
    PASS / FAIL / SKIP / TIMEOUT."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(root),
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", errors="replace")
        return "TIMEOUT", 0, partial
    text = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        return "FAIL", 0, text
    if SKIP_RE.search(text):
        return "SKIP", 0, text
    match = RAN_RE.search(text)
    return "PASS", int(match.group(1)) if match else 0, text


def main(argv=None):
    parser = argparse.ArgumentParser(description="Per-file test runner (each test file runs as its own script).")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="seconds per test file (default %(default)s)")
    parser.add_argument("files", nargs="*", help="only these test files (relative to the root)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    tests = find_tests(root, args.files)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "TIMEOUT": 0}
    ran = 0
    if not tests:
        print("no test files found under %s" % root)
        print("TOTAL files=0 pass=0 fail=0 skip=0 timeout=0 tests=0")
        return 1
    for path in tests:
        status, tests_run, output = run_file(path, root, args.timeout)
        counts[status] += 1
        ran += tests_run
        print("%-7s %s (%d tests)" % (status, path.relative_to(root).as_posix(), tests_run))
        if status in ("FAIL", "TIMEOUT"):
            print(output.rstrip())
    red = counts["FAIL"] + counts["TIMEOUT"]
    print("TOTAL files=%d pass=%d fail=%d skip=%d timeout=%d tests=%d" % (
        len(tests), counts["PASS"], counts["FAIL"], counts["SKIP"], counts["TIMEOUT"], ran))
    return 1 if red else 0


if __name__ == "__main__":
    reconfigure_stdio()
    sys.exit(main())
