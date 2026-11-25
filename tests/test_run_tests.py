"""Unit tests for run_tests.py (SKIP-FILE protocol, stdin, non-UTF-8 bytes, exit code).

Each case builds its OWN temp tree and runs the real runner on it as a subprocess.
Run as a script: ``python tests/test_run_tests.py``.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parent.parent / "run_tests.py"


def make_tree(files):
    """Create a temp root with the given {relative path: file bytes}."""
    root = Path(tempfile.mkdtemp(prefix="rvs-runner-"))
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return root


def run_runner(root, *extra):
    proc = subprocess.run(
        [sys.executable, str(RUNNER), "--root", str(root), *extra],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=120,
    )
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


GREEN = "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\nunittest.main()\n"


class RunnerTests(unittest.TestCase):
    def test_all_green_exit_zero_and_totals_last_line(self):
        root = make_tree({"tests/test_a.py": GREEN, "scripts/tests/test_b.py": GREEN})
        rc, out = run_runner(root)
        self.assertEqual(rc, 0, out)
        last = out.strip().splitlines()[-1]
        self.assertEqual(last, "TOTAL files=2 pass=2 fail=0 skip=0 timeout=0 tests=2")

    def test_red_file_makes_exit_nonzero(self):
        root = make_tree({"tests/test_a.py": GREEN, "tests/test_bad.py": "raise SystemExit(3)\n"})
        rc, out = run_runner(root)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL    tests/test_bad.py", out)
        self.assertIn("fail=1", out.strip().splitlines()[-1])

    def test_skip_file_marker_at_rc0_is_skip(self):
        root = make_tree({"tests/test_s.py": "print('SKIP-FILE: no browser here')\n"})
        rc, out = run_runner(root)
        self.assertEqual(rc, 0, out)
        self.assertIn("SKIP    tests/test_s.py", out)

    def test_skip_marker_is_ignored_at_nonzero_rc(self):
        root = make_tree({"tests/test_s.py": "print('SKIP-FILE: x')\nraise SystemExit(1)\n"})
        rc, out = run_runner(root)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL    tests/test_s.py", out)

    def test_skip_marker_must_start_the_line(self):
        root = make_tree({"tests/test_s.py": "print('note: SKIP-FILE: x')\n"})
        rc, out = run_runner(root)
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS    tests/test_s.py", out)

    def test_child_gets_no_stdin(self):
        root = make_tree({"tests/test_in.py": "import sys\nassert sys.stdin.read() == ''\n"})
        rc, out = run_runner(root)
        self.assertEqual(rc, 0, out)

    def test_non_utf8_child_output_does_not_crash_runner(self):
        code = b"import sys\nsys.stdout.buffer.write(b'caf\\xe9 \\xff\\n')\nraise SystemExit(1)\n"
        root = make_tree({"tests/test_bytes.py": code})
        rc, out = run_runner(root)
        self.assertEqual(rc, 1, out)
        self.assertIn("FAIL    tests/test_bytes.py", out)

    def test_child_sees_utf8_io_encoding(self):
        root = make_tree({"tests/test_enc.py": "import os\nassert os.environ['PYTHONIOENCODING'] == 'utf-8'\n"})
        rc, out = run_runner(root)
        self.assertEqual(rc, 0, out)

    def test_timeout_is_red(self):
        root = make_tree({"tests/test_slow.py": "import time\ntime.sleep(30)\n"})
        rc, out = run_runner(root, "--timeout", "1")
        self.assertEqual(rc, 1, out)
        self.assertIn("TIMEOUT tests/test_slow.py", out)

    def test_no_test_files_is_red(self):
        root = make_tree({"README.txt": "nothing"})
        rc, out = run_runner(root)
        self.assertEqual(rc, 1, out)

    def test_only_listed_file_runs(self):
        root = make_tree({"tests/test_a.py": GREEN, "tests/test_bad.py": "raise SystemExit(3)\n"})
        rc, out = run_runner(root, "tests/test_a.py")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("test_bad", out)


if __name__ == "__main__":
    unittest.main()
