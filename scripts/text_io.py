"""text_io.py — text input/output helpers shared by the command-line scripts.

Windows consoles and redirects default to a legacy code page (cp1252, cp949, ...) and PowerShell 5.1
writes UTF-16 files, so a Vietnamese/Korean message or a captured log can crash or be misread unless
every script says what encoding it means. Call `force_utf8_stdio()` first on the `__main__` path of every
script (never inside `main()`: tests call `main()` with an `io.StringIO` that has no `reconfigure`).
"""
from __future__ import annotations

import codecs
import sys
from pathlib import Path


def force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 whatever the console code page is; undecodable output is replaced, never raised."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def read_text_sniffed(path: str | Path, *, strict: bool = False) -> str:
    """Read a text file written by any common Windows tool: UTF-8 (with or without BOM), UTF-16 with a BOM
    (PowerShell 5.1 redirects and `Out-File`), or BOM-less UTF-16-LE (NUL bytes at the start).

    Default: undecodable bytes become U+FFFD instead of raising — right for a LOG, where only the ASCII
    `MARK` lines matter. `strict=True` raises UnicodeDecodeError instead — right for text that ends up in a
    video (the narration), where a silent U+FFFD would burn a corrupted subtitle."""
    errors = "strict" if strict else "replace"
    data = Path(path).read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8):].decode("utf-8", errors=errors)
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", errors=errors)
    if b"\x00" in data[:512]:
        return data.decode("utf-16-le", errors=errors)
    return data.decode("utf-8", errors=errors)
