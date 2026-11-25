#!/usr/bin/env bash
# Run the per-file test runner with whichever Python is available.
here="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$here/run_tests.py" "$@"
fi
exec python "$here/run_tests.py" "$@"
