#!/usr/bin/env python3
"""demo_template.py — starting point of a demo/manual take: log in to a DEMO Odoo, then walk a guided tour.

Copy this file next to your work (never edit the plugin's own copy), change TOUR and NARRATION, run it with the
Python that has Playwright (Windows: `py -3 demo_template.py`):

    py -3 demo_template.py --out-dir videos --lang vi

Credentials come ONLY from environment variables the user set in the terminal that started Claude Code:
    ODOO_DEMO_URL        address of the DEMO system, e.g. https://your-odoo-instance
    ODOO_DEMO_LOGIN      login of a demo account
    ODOO_DEMO_PASSWORD   its password
    ODOO_DEMO_APP        (optional) name of the app to open in the tour
ONLY a throwaway DEMO account on a DEMO system, never production and never a password used anywhere else: the
variables live in the environment of the terminal that started Claude Code, so everything Claude runs there can read
them. The URL must be https (plain http only for localhost) and must not contain a login.
This script never prints, logs or writes any of their values (only the variable NAMES when one is missing), removes
the password and Playwright's debug switches (DEBUG, PWDEBUG ...) from its own environment before any browser or
ffmpeg process starts, types the password through the real keyboard into a field the browser already hides, refuses
to type it into a page on another host than ODOO_DEMO_URL, puts an overlay over every password field so it can never
be read from the video, and deletes the temporary browser profile (it holds the demo session) when the run ends. The raw capture still contains the login frames:
`burn_subtitles.py --cut` removes them from the final video; delete raw captures after the final video is made.

Rules of a tour (the skill's hard rules): the login lead-in may use `page.goto`; after the FIRST `MARK` every screen
is reached with the cursor (helpers), never by URL; every click goes through a helper, never `.click()`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def _scripts_dir() -> Path:
    """The plugin's scripts folder: ${CLAUDE_PLUGIN_ROOT}/scripts when that variable is set, otherwise relative to this
    file (skills/record-demo-video/ -> ../../scripts)."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    for candidate in ([Path(root) / "scripts"] if root else []) + [Path(__file__).resolve().parents[2] / "scripts"]:
        if (candidate / "recorder.py").is_file():
            return candidate
    raise SystemExit("demo_template: cannot find the plugin's scripts folder; set CLAUDE_PLUGIN_ROOT")


sys.path.insert(0, str(_scripts_dir()))

from click_helpers import click_box, point_at, sidebar, type_into, wait_ready  # noqa: E402
from stealth_recorder import make_recorder  # noqa: E402
from tour_helpers import fill_narration, mask_input_overlay_js  # noqa: E402

REQUIRED_ENV = ("ODOO_DEMO_URL", "ODOO_DEMO_LOGIN", "ODOO_DEMO_PASSWORD")
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# Variables that make Playwright log protocol traffic (including every typed key) or open its inspector.
PLAYWRIGHT_DEBUG_ENV = ("DEBUG", "DEBUG_FILE", "PWDEBUG", "PWDEBUGIMPL")

# Every language of the video, one numbered instruction per click, then what the screen is for. `{S}` = step number.
NARRATION_TEMPLATE = {
    "home": {"vi": "Đây là màn hình chính. Chúng ta sẽ đi từng bước bằng các menu thật.",
             "en": "This is the home screen. We will go step by step through the real menus.",
             "ko": "메인 화면입니다. 실제 메뉴를 따라 단계별로 이동해 보겠습니다."},
    "click_app": {"vi": "Bước {S}: ở thanh bên trái, bấm vào ứng dụng của chúng ta.",
                  "en": "Step {S}: in the left sidebar, click our app.",
                  "ko": "{S}단계: 왼쪽 사이드바에서 해당 앱을 클릭합니다."},
    "done": {"vi": "Đến đây là hết phần hướng dẫn.",
             "en": "That is the end of this walkthrough.",
             "ko": "여기까지 안내를 마치겠습니다."},
}


def load_credentials(environ=None) -> dict:
    """The demo settings from the environment. A missing variable stops the run naming the VARIABLE, never a value."""
    environ = os.environ if environ is None else environ
    missing = [name for name in REQUIRED_ENV if not environ.get(name)]
    if missing:
        raise SystemExit("demo_template: set these environment variables in the terminal that started Claude Code "
                         "and start it again from that terminal: " + ", ".join(missing))
    url = environ["ODOO_DEMO_URL"].rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise SystemExit("demo_template: ODOO_DEMO_URL must be an http(s) address with a host name")
    if parts.username is not None or parts.password is not None:
        # a login embedded in the URL would be echoed by every browser error and stored in the browser profile
        raise SystemExit("demo_template: ODOO_DEMO_URL must not contain a user name or password; use ODOO_DEMO_LOGIN / "
                         "ODOO_DEMO_PASSWORD")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise SystemExit("demo_template: ODOO_DEMO_URL must be https (plain http would send the password unencrypted)")
    return {"url": url, "login": environ["ODOO_DEMO_LOGIN"], "password": environ["ODOO_DEMO_PASSWORD"],
            "app": environ.get("ODOO_DEMO_APP", "")}


def _origin(url: str) -> tuple:
    """(lower-cased host, port) with the default port filled in, so an upper-case host name or an explicit :443 equals
    the address the browser reports."""
    parts = urlsplit(url)
    return parts.hostname, parts.port or {"http": 80, "https": 443}.get(parts.scheme)


async def log_in(page, cursor, creds: dict) -> None:
    """Lead-in (before the first MARK, cut from the final video): open the login page and sign in with the cursor."""
    await page.goto(creds["url"] + "/web/login", wait_until="domcontentloaded")
    await wait_ready(page, ".o_login_form, input[name=login]", timeout=60)
    # Never type the password into a page on another host (a redirect, a typo in the address): compare hosts first.
    if _origin(page.url) != _origin(creds["url"]):
        raise SystemExit("demo_template: the login page is on a different host than ODOO_DEMO_URL; nothing was typed")
    await type_into(page, page.locator("input[name=login]"), creds["login"], cursor)
    await type_into(page, page.locator("input[name=password]"), creds["password"], cursor)
    await click_box(page, page.locator("button[type=submit]").first, cursor)
    await wait_ready(page, ".o_main_navbar", timeout=90)       # a cold web client can take a minute


async def tour(rec, page, cursor, creds: dict) -> None:
    """The guided tour. Edit this: one `await rec.mark_paced("<step>")` BEFORE each helper call, the step name a
    key of NARRATION_TEMPLATE. Reach every screen with the cursor (sidebar / navbar / dropdown_item / menu_path /
    press_button), never by URL."""
    await rec.mark_paced("home")
    await point_at(page, page.locator(".o_main_navbar").first, cursor, hold=2.0)
    if creds["app"]:
        await rec.mark_paced("click_app")
        await sidebar(page, cursor, creds["app"])
    await rec.mark_paced("done")


async def run(out_dir: Path, langs: list[str], capture: str) -> int:
    creds = load_credentials()
    # From here on nothing that starts a child process (the Playwright driver, Chromium, ffmpeg) should inherit the
    # password, and no Playwright debug switch may log the typed keys to the terminal.
    for name in PLAYWRIGHT_DEBUG_ENV + ("ODOO_DEMO_PASSWORD",):
        os.environ.pop(name, None)
    print(f"target host: {urlsplit(creds['url']).hostname}  (use a DEMO system and a throwaway demo account only)")
    out_dir.mkdir(parents=True, exist_ok=True)
    narration = fill_narration({k: {lang: text for lang, text in v.items() if lang in langs}
                                for k, v in NARRATION_TEMPLATE.items()})
    (out_dir / "narration.json").write_text(json.dumps(narration, ensure_ascii=False, indent=2), encoding="utf-8")
    profile_dir = tempfile.mkdtemp(prefix="demo-profile-")      # isolated profile, never the user's real browser
    try:
        rec = make_recorder(":99", str(out_dir / "raw_capture.mp4"), profile_dir, driver="chrome", capture=capture)
        rec.patch_chrome_prefs(lang=langs[0])
        async with rec.session() as (page, cursor):
            # Overlay over every password field (by type or by name, so a "show password" toggle cannot expose it):
            # its value can never be read from the video (the overlay never writes `.value`). Registered BEFORE the
            # first navigation of the take.
            await page.context.add_init_script(script=mask_input_overlay_js(
                r".+", "••••••••",
                selector="input[type=password], input[name=password]"))
            await log_in(page, cursor, creds)
            rec.pace_from(narration)
            await tour(rec, page, cursor, creds)
    finally:
        # The profile holds the demo account's live session (cookies, history): never leave it behind.
        shutil.rmtree(profile_dir, ignore_errors=True)
        if os.path.exists(profile_dir):         # Windows can keep a file locked for a moment
            print(f"note: could not delete the temporary browser profile {profile_dir}; delete it by hand "
                  "(it holds the demo session)")
    print(f"raw capture: {out_dir / 'raw_capture.mp4'}")
    print(f"narration:   {out_dir / 'narration.json'}")
    print("next: build_srt.py --log <this run's output> --narration narration.json --lang <lang> --out out_<lang>.srt")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record a guided tour of a demo Odoo (credentials from ODOO_DEMO_*).")
    ap.add_argument("--out-dir", default=".", help="folder for raw_capture.mp4 and narration.json")
    ap.add_argument("--lang", default="vi", help="narration languages, comma separated (vi,ko,en); the first one sets the browser language")
    ap.add_argument("--capture", default="auto", choices=("auto", "video", "x11"),
                    help="auto = Playwright video on Windows/macOS, Xvfb on Linux")
    args = ap.parse_args(argv)
    langs = [lang.strip() for lang in args.lang.split(",") if lang.strip()]
    return asyncio.run(run(Path(args.out_dir), langs, args.capture))


if __name__ == "__main__":
    from text_io import force_utf8_stdio

    force_utf8_stdio()
    sys.exit(main())
