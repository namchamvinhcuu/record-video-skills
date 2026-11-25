#!/usr/bin/env python3
"""check_env.py — is this machine ready to record a manual video? (stdlib only)

Prints one line per prerequisite: `OK` / `MISSING` / `SKIP`, and for every MISSING item the exact command that
fixes it (Windows commands first). Exit code 0 only when nothing is MISSING.

Checks: Python 3.10+, the `playwright` package and its Chromium, Pillow, ffmpeg with the libass `subtitles` filter
AND the libx264 encoder, ffprobe, a Korean-capable font, free disk space. Every check is a pure function that takes its
probes as arguments, so the tests need neither Windows nor a browser.

Run: python check_env.py        (Windows: py -3 check_env.py, or the check-env.cmd launcher)
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from fonts_probe import FONT_CHAINS, ffmpeg_features, pick_font

MIN_PYTHON = (3, 10)
MIN_FREE_GB = 2.0
_WINGET_FLAGS = "--accept-source-agreements --accept-package-agreements"   # a first run would otherwise wait for a prompt
WINGET_PYTHON = f"winget install Python.Python.3.12 {_WINGET_FLAGS}"
WINGET_FFMPEG = f"winget install Gyan.FFmpeg {_WINGET_FLAGS}"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None          # True = OK, False = MISSING, None = SKIP (could not be checked on this machine)
    detail: str
    fix: str = ""


def check_python(version_info=None) -> Check:
    """The interpreter running this script."""
    major, minor = (version_info or sys.version_info)[:2]
    found = f"Python {major}.{minor}"
    if (major, minor) >= MIN_PYTHON:
        return Check("python", True, found)
    return Check("python", False, f"{found} is too old (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+)", WINGET_PYTHON)


_PYTHON_VERSION = re.compile(r"^Python (\d+)\.(\d+)", re.MULTILINE)


def find_python(which=shutil.which, run=subprocess.run) -> Check:
    """A usable Python 3.10+ launcher on PATH: tries `py -3`, `python`, `python3` and checks the return code AND the
    printed version, never `where`. The Microsoft Store `python` alias is a stub that exits 9009 and prints a hint, so
    it counts as missing."""
    for command in (["py", "-3"], ["python"], ["python3"]):
        if which(command[0]) is None:
            continue
        try:
            proc = run(command + ["--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        out = proc.stdout.decode("utf-8", errors="replace") if isinstance(proc.stdout, bytes) else str(proc.stdout or "")
        match = _PYTHON_VERSION.search(out)
        if proc.returncode == 0 and match and (int(match.group(1)), int(match.group(2))) >= MIN_PYTHON:
            return Check("python launcher", True, f"`{' '.join(command)}` → Python {match.group(1)}.{match.group(2)}")
    return Check("python launcher", False, "no working Python 3.10+ on PATH (the Microsoft Store `python` stub does not count)",
                 WINGET_PYTHON + "   (then open a NEW terminal)")


def check_playwright(find_spec=importlib.util.find_spec, chromium_path=None) -> Check:
    """The `playwright` package and the Chromium it drives. `chromium_path` (a callable returning the browser path)
    is injected by tests; the default asks Playwright itself."""
    if find_spec("playwright") is None:
        return Check("playwright", False, "the `playwright` package is not installed",
                     "py -3 -m pip install playwright   then   py -3 -m playwright install chromium")
    try:
        path = (chromium_path or _playwright_chromium_path)()
    except Exception as exc:  # noqa: BLE001 — any driver failure is a MISSING with its reason, never a crash
        return Check("playwright", False, f"cannot ask Playwright for its Chromium ({type(exc).__name__}: {str(exc)[:120]})",
                     "py -3 -m playwright install chromium")
    if path and os.path.exists(path):
        return Check("playwright", True, "package + Chromium present")
    return Check("playwright", False, "the package is installed but its Chromium is not", "py -3 -m playwright install chromium")


def _playwright_chromium_path() -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return p.chromium.executable_path


def check_ffmpeg(which=shutil.which, run=None) -> Check:
    """ffmpeg on PATH with the libass `subtitles` filter (burning .srt) and the libx264 encoder (H.264 mp4)."""
    if which("ffmpeg") is None:
        return Check("ffmpeg", False, "ffmpeg is not on PATH", WINGET_FFMPEG + "   (then open a NEW terminal)")
    features = ffmpeg_features(run=run)
    if features["error"]:
        return Check("ffmpeg", False, f"ffmpeg cannot be started ({features['error']})", WINGET_FFMPEG)
    lacking = [label for key, label in (("subtitles", "the libass `subtitles` filter"), ("libx264", "the libx264 encoder"))
               if features[key] is False]
    if lacking:
        return Check("ffmpeg", False, "this ffmpeg build lacks " + " and ".join(lacking),
                     WINGET_FFMPEG + "   (a FULL build; a minimal build has no libass)")
    if features["subtitles"] is None or features["libx264"] is None:
        return Check("ffmpeg", None, "ffmpeg is present but its filter/encoder list could not be read")
    return Check("ffmpeg", True, "libass `subtitles` filter + libx264 present")


def _trim_only(platform: str | None) -> bool:
    """trim_black_tail.py cuts the BLACK tail of an x11grab capture (Linux). The Playwright video path (Windows,
    macOS) ends in a frozen frame that script cannot cut, so the workflow skips it there."""
    return (platform or sys.platform) in ("win32", "darwin")


def check_pillow(find_spec=importlib.util.find_spec, platform: str | None = None) -> Check:
    """Pillow reads the frames trim_black_tail.py samples; Playwright does not install it. Required for the Linux
    capture; on Windows/macOS only informational (SKIP when missing)."""
    if find_spec("PIL") is None:
        if _trim_only(platform):
            return Check("pillow", None, "not installed — only trim_black_tail.py (Linux capture) needs it; not used here")
        return Check("pillow", False, "the `pillow` package is not installed (trim_black_tail.py needs it)",
                     "py -3 -m pip install pillow")
    return Check("pillow", True, "package present")


def check_ffprobe(which=shutil.which, platform: str | None = None) -> Check:
    """ffprobe measures the video for trim_black_tail.py; it ships with the full ffmpeg package. Required for the Linux
    capture; on Windows/macOS only informational (SKIP when missing)."""
    if which("ffprobe") is None:
        if _trim_only(platform):
            return Check("ffprobe", None, "not on PATH — only trim_black_tail.py (Linux capture) needs it; not used here")
        return Check("ffprobe", False, "ffprobe is not on PATH (it comes with the full ffmpeg package)",
                     WINGET_FFMPEG + "   (then open a NEW terminal)")
    return Check("ffprobe", True, "found on PATH")


def check_font(platform=None, families_fn=None, which=shutil.which, run=subprocess.run) -> Check:
    """A Korean-capable font (it also covers Vietnamese). Windows: the first installed family of the Korean chain.
    Elsewhere: `fc-list :lang=ko` when fontconfig is available, else SKIP."""
    platform = platform or sys.platform
    chain = ", ".join(FONT_CHAINS["ko"][:2])
    if platform == "win32":
        found = pick_font("ko", "", platform=platform, families_fn=families_fn)
        if found:
            return Check("font", True, f"{found} is installed")
        return Check("font", False, f"none of {chain} is installed — Korean subtitles would show empty boxes",
                     "install the Korean language pack (Windows Settings → Time & language) or Noto Sans CJK KR")
    if which("fc-list") is None:
        return Check("font", None, "fontconfig (`fc-list`) not available — font not checked on this platform")
    try:
        proc = run(["fc-list", ":lang=ko", "family"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return Check("font", None, "`fc-list` failed — font not checked")
    families = proc.stdout.decode("utf-8", errors="replace").strip() if isinstance(proc.stdout, bytes) else ""
    if families:
        return Check("font", True, families.splitlines()[0].strip())
    return Check("font", False, "no Korean-capable font found by fontconfig", "install Noto Sans CJK KR")


def check_disk(path: str | None = None, min_gb: float = MIN_FREE_GB, usage=shutil.disk_usage) -> Check:
    """Free space where recordings are written (raw captures are large)."""
    where = path or tempfile.gettempdir()
    try:
        free_gb = usage(where).free / 1024 ** 3
    except OSError as exc:
        return Check("disk", None, f"cannot read free space of {where} ({exc})")
    if free_gb >= min_gb:
        return Check("disk", True, f"{free_gb:.1f} GB free in {where}")
    return Check("disk", False, f"only {free_gb:.1f} GB free in {where} (need {min_gb:g} GB)", "free some disk space")


def run_checks() -> list[Check]:
    return [check_python(), find_python(), check_playwright(), check_pillow(), check_ffmpeg(), check_ffprobe(),
            check_font(), check_disk()]


def format_report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        status = {True: "OK     ", False: "MISSING", None: "SKIP   "}[c.ok]
        lines.append(f"{status} {c.name}: {c.detail}")
        if c.ok is False and c.fix:
            lines.append(f"        fix: {c.fix}")
    missing = sum(1 for c in checks if c.ok is False)
    lines.append("READY: all prerequisites found." if not missing else f"NOT READY: {missing} prerequisite(s) MISSING.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description="Check this machine for the record-demo-video prerequisites.").parse_args(argv)
    checks = run_checks()
    print(format_report(checks))
    return 1 if any(c.ok is False for c in checks) else 0


if __name__ == "__main__":
    from text_io import force_utf8_stdio

    force_utf8_stdio()
    sys.exit(main())
