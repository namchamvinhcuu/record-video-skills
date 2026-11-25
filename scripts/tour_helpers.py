"""Guided-tour script helpers for record-demo-video — plain functions, no browser needed.

- fill_narration: `{S}` auto step numbers + `{name}` values (per language) in a narration dict.
- format_number: a number the way each language writes it (vi `1.000.000`, en/ko `1,000,000`).
- mask_text_js: an init script that rewrites sensitive text on the page (bank accounts, personal data).

See REFERENCE.md section 9. Narration dict shape = the one build_srt.py reads: {step: {lang: text}}.
"""
from __future__ import annotations

import json
import re

_LEFTOVER = re.compile(r"\{[A-Za-z_]\w*\}")


def format_number(value, lang: str) -> str:
    """1000000 -> '1.000.000' for vi, '1,000,000' for every other language."""
    text = f"{value:,}"
    return text.replace(",", ".") if lang == "vi" else text


def fill_narration(narration: dict, values: dict | None = None, step_token: str = "{S}") -> dict:
    """Return a COPY of `narration` with step numbers and values filled in.

    - `step_token` ("{S}") counts the STEPS that contain it, in dict order: the first such step
      becomes 1 in every language, the next 2, ... — adding a scene never means renumbering.
    - `values` = {name: text} or {name: {lang: text}}; "{name}" in a cue is replaced (a plain
      text applies to every language). Use format_number() to build per-language numbers.
    - A `{word}` left over after filling raises ValueError naming it (a forgotten value must not
      reach the subtitles).
    """
    values = values or {}
    filled: dict = {}
    counter = 0
    for step, texts in narration.items():
        if any(step_token in t for t in texts.values()):
            counter += 1
        out = {}
        for lang, text in texts.items():
            text = text.replace(step_token, str(counter))
            for name, val in values.items():
                text = text.replace("{%s}" % name, val[lang] if isinstance(val, dict) else str(val))
            left = _LEFTOVER.findall(text)
            if left:
                raise ValueError(f"fill_narration: step {step!r} [{lang}] still has {left} after filling")
            out[lang] = text
        filled[step] = out
    return filled


def mask_text_js(pattern: str, replacement: str, interval_ms: int = 100, observe: bool = True) -> str:
    """JavaScript for `page.add_init_script(script=...)` that rewrites every text node matching the
    JS regex source `pattern` with `replacement` ("$1" groups allowed). `observe=True` also installs a
    MutationObserver on the document: a node added or changed by Odoo is masked in the same task,
    BEFORE the next paint (the interval alone showed the real value for up to `interval_ms`). The
    interval stays as a safety net. Only text NODES are rewritten, never an <input>/<textarea> value
    (Odoo would SAVE a rewritten value into the record): use mask_input_overlay_js for those.
    Example: mask_text_js(r"\\b5500\\d{4}(\\d{4})\\b", "********$1")."""
    observer = (
        "  const obs = new MutationObserver((muts) => {\n"
        "    for (const m of muts) {\n"
        "      if (m.type === 'characterData') maskNode(m.target);\n"
        "      else m.addedNodes.forEach(maskTree);\n"
        "    }\n"
        "    obs.takeRecords();  // drop our own writes: a replacement that matches the pattern must not loop\n"
        "  });\n"
        "  obs.observe(document, {childList: true, subtree: true, characterData: true});\n"
    ) if observe else ""
    return (
        "(() => {\n"
        f"  const re = new RegExp({json.dumps(pattern)}, 'g');\n"
        f"  const rep = {json.dumps(replacement)};\n"
        "  const maskNode = (n) => {\n"
        "    const v = n.nodeValue, m = v.replace(re, rep);\n"
        "    if (m !== v) n.nodeValue = m;\n"
        "  };\n"
        "  const maskTree = (root) => {\n"
        "    if (root.nodeType === 3) { maskNode(root); return; }\n"
        "    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);\n"
        "    for (let n = w.nextNode(); n; n = w.nextNode()) maskNode(n);\n"
        "  };\n"
        "  const mask = () => {\n"
        "    if (!document.body) return;\n"
        "    maskTree(document.body);\n"
        "  };\n"
        f"{observer}"
        f"  setInterval(mask, {int(interval_ms)});\n"
        "})();\n"
    )


def mask_input_overlay_js(pattern: str, replacement: str, selector: str = "input, textarea",
                          interval_ms: int = 30) -> str:
    """JavaScript for `page.add_init_script(script=...)` that hides the real value of matching
    <input>/<textarea> elements by drawing an OVERLAY element on top of each one — the input's own
    `.value` is never written (Odoo saves a rewritten value into the real record on the next button
    press). The overlay copies the input's rect, font, colours and border and shows the value with
    `pattern` (JS regex source) replaced by `replacement`; it follows the input on scroll/resize and
    disappears when the value no longer matches or the input leaves the page. Overlays carry
    `data-demo-mask` and `pointer-events: none`. Pair it with assert_form_clean()."""
    return (
        "(() => {\n"
        f"  const re = new RegExp({json.dumps(pattern)}, 'g');\n"
        f"  const rep = {json.dumps(replacement)};\n"
        f"  const SEL = {json.dumps(selector)};\n"
        "  const overlays = new Map();\n"
        "  const bgOf = (el) => {\n"
        "    for (let e = el; e; e = e.parentElement) {\n"
        "      const c = getComputedStyle(e).backgroundColor;\n"
        "      if (c && c !== 'transparent' && c !== 'rgba(0, 0, 0, 0)') return c;\n"
        "    }\n"
        "    return '#fff';\n"
        "  };\n"
        "  const drop = (el) => { const o = overlays.get(el); if (o) { o.remove(); overlays.delete(el); } };\n"
        "  const sync = () => {\n"
        "    if (!document.body) return;\n"
        "    for (const el of Array.from(overlays.keys())) if (!el.isConnected) drop(el);\n"
        "    for (const el of document.querySelectorAll(SEL)) {\n"
        "      const v = typeof el.value === 'string' ? el.value : '';\n"
        "      const m = v.replace(re, rep);\n"
        "      const r = el.getBoundingClientRect();\n"
        "      if (m === v || r.width === 0 || r.height === 0) { drop(el); continue; }\n"
        "      let o = overlays.get(el);\n"
        "      if (!o) {\n"
        "        o = document.createElement('div');\n"
        "        o.setAttribute('data-demo-mask', '');\n"
        "        document.body.appendChild(o);\n"
        "        overlays.set(el, o);\n"
        "      }\n"
        "      const cs = getComputedStyle(el);\n"
        "      o.textContent = m;\n"
        "      o.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483647;overflow:hidden;'\n"
        "        + 'white-space:pre;display:flex;align-items:center;box-sizing:border-box;'\n"
        "        + `left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;`\n"
        "        + `font:${cs.font};color:${cs.color};padding:${cs.padding};border:${cs.border};`\n"
        "        + `border-radius:${cs.borderRadius};background:${bgOf(el)};`;\n"
        "    }\n"
        "  };\n"
        "  addEventListener('scroll', sync, true);\n"
        "  addEventListener('resize', sync);\n"
        f"  setInterval(sync, {int(interval_ms)});\n"
        "})();\n"
    )


FORM_DIRTY_SELECTOR = ".o_form_status_indicator_buttons:not(.invisible)"   # Odoo 17 form_status_indicator.xml


async def assert_form_clean(page, selector: str = FORM_DIRTY_SELECTOR) -> None:
    """Raise RuntimeError when the open Odoo form has unsaved changes (the Save/Discard indicator is
    shown). Call it before a button press or navigation in a take that masks values: a dirty form is
    saved by the click and the masked text could end up in the real record. A NEW record counts as
    dirty in Odoo 17 (its indicator is always shown)."""
    if await page.locator(selector).count():
        raise RuntimeError("assert_form_clean: the form has unsaved changes — a click would save them")
