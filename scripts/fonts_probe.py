"""fonts_probe.py — which subtitle font to use per language, and what this machine's ffmpeg can do.

Shared by burn_subtitles.py (font choice, readable message when ffmpeg cannot burn subtitles) and by
check_env.py (the same probes, run before a long take). Pure functions take their data as arguments so the
tests need neither Windows nor ffmpeg.

Font choice is Windows-only on purpose: on every other platform the caller keeps its own default font
(`Noto Sans CJK KR`), exactly as before. On Windows the system fonts are looked up by family NAME in the
registry (no subprocess, no stderr) and the first family of the language's chain that is installed wins.
"""
from __future__ import annotations

import re
import subprocess
import sys

# Preferred family first. Windows ships Malgun Gothic (Korean) and Segoe UI (Vietnamese/Latin) and Arial.
FONT_CHAINS = {
    "ko": ("Malgun Gothic", "Noto Sans CJK KR", "Noto Sans KR", "Gulim"),
    "vi": ("Segoe UI", "Arial", "Tahoma", "Noto Sans CJK KR"),
    "en": ("Segoe UI", "Arial", "Noto Sans CJK KR"),
}
FALLBACK_CHAIN = ("Segoe UI", "Arial", "Malgun Gothic")   # a language with no chain of its own

_REGISTRY_FONT_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_TYPE_SUFFIX = re.compile(r"\s*\((?:TrueType|OpenType|All res)\)\s*$", re.IGNORECASE)
_STYLE_SUFFIX = re.compile(r"\s+(?:Bold Italic|Bold|Italic|Regular|Light|Semilight|Semibold|Black|Medium|Thin)$",
                           re.IGNORECASE)


def families_from_registry_names(names) -> set[str]:
    """Family names from Windows font registry value names such as `Malgun Gothic Bold (TrueType)` or
    `MS Gothic & MS UI Gothic (TrueType)`. Both the full face name and the name without its style suffix
    are returned (`Segoe UI Semibold` → `Segoe UI Semibold`, `Segoe UI`)."""
    families: set[str] = set()
    for raw in names:
        name = _TYPE_SUFFIX.sub("", str(raw)).strip()
        for part in name.split(" & "):
            part = part.strip()
            if not part:
                continue
            families.add(part)
            base = part
            while True:
                stripped = _STYLE_SUFFIX.sub("", base)
                if stripped == base:
                    break
                base = stripped
                families.add(base)
    return families


def windows_font_families() -> set[str]:
    """Installed font families on Windows (machine and per-user registry keys). Empty set when `winreg` is
    unavailable (not Windows) or a key cannot be read."""
    try:
        import winreg
    except ImportError:
        return set()
    names: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, _REGISTRY_FONT_KEY) as key:
                index = 0
                while True:
                    try:
                        names.append(winreg.EnumValue(key, index)[0])
                    except OSError:      # no more values
                        break
                    index += 1
        except OSError:
            continue
    return families_from_registry_names(names)


def pick_font(lang: str, default: str, *, platform: str | None = None, families_fn=None) -> str:
    """The font to burn `lang` subtitles with. Not Windows → `default`, untouched. Windows → the first family of
    the language's chain that is installed (case-insensitive), else `default`."""
    if (platform or sys.platform) != "win32":
        return default
    installed = {family.lower() for family in (families_fn or windows_font_families)()}
    for name in FONT_CHAINS.get(lang, FALLBACK_CHAIN):
        if name.lower() in installed:
            return name
    return default


def _text(proc) -> str | None:
    out = getattr(proc, "stdout", None)
    if isinstance(out, bytes):
        return out.decode("utf-8", errors="replace")
    return out if isinstance(out, str) else None


def ffmpeg_features(ffmpeg: str = "ffmpeg", *, run=None) -> dict:
    """What this ffmpeg build can do: {"subtitles": bool|None, "libx264": bool|None, "error": str|None}.
    `subtitles` is the libass filter (burning an .srt needs it), `libx264` the H.264 encoder. None = could not be
    determined (the probe output was not readable); `error` is set when ffmpeg cannot be started at all."""
    run = run or subprocess.run
    result: dict = {"subtitles": None, "libx264": None, "error": None}
    for key, flag, pattern in (("subtitles", "-filters", r"^\s*\S+\s+subtitles\s"),
                               ("libx264", "-encoders", r"^\s*\S+\s+libx264\s")):
        try:
            proc = run([ffmpeg, "-hide_banner", flag], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        text = _text(proc)
        # a probe that exited non-zero says nothing about the build: leave the answer unknown (None)
        if text is not None and getattr(proc, "returncode", 0) == 0:
            result[key] = re.search(pattern, text, re.MULTILINE) is not None
    return result


def explain_ffmpeg_failure(ffmpeg: str = "ffmpeg", *, run=None) -> str | None:
    """A readable reason when this ffmpeg build cannot burn subtitles (no libass `subtitles` filter or no
    libx264), else None (also None when the probe could not tell). Meant for AFTER a failed run, never the
    success path."""
    features = ffmpeg_features(ffmpeg, run=run)
    if features["error"]:
        return f"ffmpeg cannot be started ({features['error']}) — install it and reopen the terminal"
    missing = [label for key, label in (("subtitles", "the libass `subtitles` filter"),
                                        ("libx264", "the libx264 encoder")) if features[key] is False]
    if not missing:
        return None
    return ("this ffmpeg build lacks " + " and ".join(missing) + " — install a full build "
            "(Windows: winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements, "
            "then reopen the terminal)")
