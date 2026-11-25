---
name: record-demo-video
description: Quy trình CHUẨN quay video hướng dẫn sử dụng (user manual) / demo bằng trình duyệt thật cho Odoo hoặc web bất kỳ — chạy trên Windows 11 (Chromium + Playwright tự quay video, không cần quyền quay màn hình), con trỏ mũi tên + gợn sóng khi click, phụ đề burn-in bằng ffmpeg, mỗi ngôn ngữ một video (vi/ko/en). Use when user nói "quay video hướng dẫn", "quay video demo", "làm video manual / hướng dẫn sử dụng", "quay thao tác Odoo/web thành video", "video đi qua từng menu", "click từng nút để giải thích chức năng cho người dùng", "사용 설명서 영상", "매뉴얼 영상 녹화". KHÔNG dùng khi chỉ cần ảnh tĩnh UI, tài liệu PowerPoint, hoặc chỉ test/screenshot UI để debug mà không xuất video cuối.
---

# record-demo-video — user-manual videos recorded with a real browser

A guided tour of a screen flow, recorded with a real isolated browser: a visible arrow cursor, one narration cue per click, subtitles burned into the video, ONE video per language. The browser runs by itself with its own temporary profile — it never records the user's real screen and never touches the user's real browser profile.

**Before anything else run `/record-video-skills:check-env`** (it checks Python, Playwright + Chromium, Pillow, ffmpeg with libass + libx264, ffprobe, a Korean-capable font and free disk, and prints the exact install command for every missing item). Install nothing (winget, pip, Playwright browsers) without the user's explicit yes. Use ONLY a throwaway demo account on a demo system: a manual video shows whatever data is on screen (the demo LOGIN NAME is typed visibly in the lead-in; the password field is covered), and the demo login lives in the environment of the terminal that started Claude Code.

## Interpreter and paths

- Windows: run scripts with `py -3 <script>` (or `python <script>`); macOS/Linux: `python3 <script>`. Scripts live in `${CLAUDE_PLUGIN_ROOT}/scripts/`; a demo script adds that folder to `sys.path` (use `pathlib` or forward slashes, never a hand-typed `C:\...` string).
- Step ids passed to `rec.mark("step_id")` must be ASCII with no spaces (a non-ASCII id can crash a Windows console redirect); narration TEXT may be Vietnamese/Korean.
- Put every intermediate file (raw capture, MARK log, srt, final) in one working folder and clean it up after export. **Delete raw captures when the final video is done**: they still contain the login frames (`burn_subtitles.py --cut` removes them only from the final video).

## How the capture works (Windows and macOS) — `capture="video"`

`DemoRecorder` picks `capture="video"` by itself on Windows and macOS: headless Chromium (the one `playwright install chromium` ships; a missing `chrome_binary` falls back to it) records the page with Playwright `record_video_dir`, and `session()` converts the webm to `out_path` (H.264 yuv420p mp4) when it exits. There is no virtual display, no `ffmpeg -f gdigrab`/`avfoundation`, no screen-recording permission. Everything downstream (`build_srt`, `burn_subtitles`) is the same on every platform.

- **Needs:** Python with `playwright` (`py -3 -m pip install playwright`, then `py -3 -m playwright install chromium`) and a full ffmpeg on PATH. `session()` checks ffmpeg before it starts, so a long take cannot be lost at the last step. (Pillow and ffprobe are needed only by `trim_black_tail.py`, which belongs to the Linux capture.)
- **MARK anchor:** MARKs count from "browser launch returned". The video runs 0.02-0.13 s AHEAD of each MARK (measured on macOS, constant over a 97 s take), so subtitles appear marginally late, never early.
- **The tail is a FROZEN last frame (0.4-5 s), not black:** `trim_black_tail.py` cannot cut it, so skip that step and choose the end cut yourself.
- **One tab only:** only the page `session()` yields is recorded; a popup / `target=_blank` / `window.open` gets its own webm that is dropped (stderr warns), so the video looks frozen. Stay in one tab.
- **If the mp4 conversion fails** (or you Ctrl-C during it) the webm is KEPT in a `rdv-video-*` folder under the temp directory and its path is in the error; convert it by hand with `ffmpeg -i <webm> -c:v libx264 -pix_fmt yuv420p out.mp4`, then delete that folder.
- **Not measured on Windows yet:** frame rate and drift of a take longer than ~100 s, smoothness of the arrow on a real Odoo page, the Windows font lookup, antivirus/OneDrive effects. Report anything odd; do not promise more than was seen.
- Linux note: `capture="x11"` (virtual display + ffmpeg x11grab) is the Linux default; the optional Firefox "stealth" driver exists only there ([REFERENCE.md](REFERENCE.md) section 0). Never use either on Windows; `make_recorder(...)` already picks the right one.

## Workflow

1. **Write the demo script from the template** — start from `demo_template.py` next to this file (copy it to the user's working folder first, never edit the plugin's copy; the copy finds the plugin's scripts through the environment variable `CLAUDE_PLUGIN_ROOT`, so run it with that variable set to `${CLAUDE_PLUGIN_ROOT}`, e.g. in PowerShell `$env:CLAUDE_PLUGIN_ROOT = "<that path>"`). It logs in to a DEMO system from the environment variables `ODOO_DEMO_URL`, `ODOO_DEMO_LOGIN`, `ODOO_DEMO_PASSWORD` (the user sets them in the terminal that started Claude Code; a password is NEVER typed into the chat, printed or written to a file), then runs the tour you edit in its `tour()` function. It creates the recorder with `make_recorder(display, out_path, profile_dir, driver="chrome")` and uses a **separate** temporary profile (NEVER the real browser profile: it carries history/extensions/logins that leak into the video).
2. **Patch the browser Preferences BEFORE launch** (the template does): `py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/patch_chrome_prefs.py" --profile-dir <profile> --lang <vi|ko|en>` — turns off the Translate popup and the "Save password?" bubble (it would show the login name in clear text).
3. **Every click MUST go through** `click_helpers.py` (`smooth_click`/`click_box`/`click_text`, and for menus and buttons `sidebar`/`navbar`/`dropdown_item`/`menu_path`/`press_button` — see Guided tour), NEVER Playwright's native `.click()` — native teleports the cursor, which breaks the cursor animation and gives away that the video is scripted. Type with `type_into()` (keystroke by keystroke), never `fill()`.
   **Scroll like a person:** when the page is longer than the screen, scroll down so the viewer sees all of it (`scroll_through(page, cursor)`); `click_box`/`click_text`/`type_into` scroll to a target outside the view with real mouse-wheel notches inside its own scroller (Odoo's `.o_content`), hit-test the click point and raise instead of clicking a covered target ([REFERENCE.md](REFERENCE.md) section 3).
   **Point, don't cover:** to show a value the narration talks about, use `point_at(page, locator, cursor)` — it parks the arrow just RIGHT of the element (left when there is no room), never on its centre. On a very wide element (a list row) it parks beside the visible part of the first text inside it; point at the cell (`td[name=…]`) that holds the value.
4. **The arrow cursor** is drawn by `cursor_overlay.js` (tip = the mouse position, a ripple on every click); `DemoRecorder.session()` injects it before the first page — never reference that file by hand.
5. **Wait for the page, never for `networkidle`:** `page.goto(url, wait_until="domcontentloaded")` then `await wait_ready(page, "<selector>")`. Odoo keeps a bus websocket open, so `networkidle` never fires. (`page.goto` is allowed only in the cut lead-in, see Guided tour.)
6. **Log step marks** with `rec.mark("step_id")` — prints `MARK <seconds since the video started> <step>`, the input for the subtitles. **Pace them for reading:** `rec.pace_from(narration)` once, then `await rec.mark_paced("step_id")` — it waits until the previous subtitle has been on screen long enough to read (max(3 s, characters / 16)).
7. **Leave `session()`** (it stops and converts the recording gracefully). Choose the lead-in cut `<cut>` = a second or so before the first `MARK`.
8. **One video per language — the default deliverable** (never stack bilingual lines). Per language:
   ```
   py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/build_srt.py" --log run.log --narration narration.json --lang vi --offset <-(cut) - 0.3> --out out_vi.srt
   py -3 "${CLAUDE_PLUGIN_ROOT}/scripts/burn_subtitles.py" --input raw_capture.mp4 --lang vi --cut <cut>    # → final_vi.mp4
   ```
   `build_srt.py` reads the log written by a Windows redirect whatever its encoding (UTF-8 or UTF-16) and stops with a clear message when the narration file is not UTF-8. `burn_subtitles.py` pads a 160 px black band BELOW the 1920x1080 capture and burns the subtitles inside it (they never cover the UI), cuts the lead-in with `-ss` placed before `-i`, and on Windows picks an installed font per language (Korean: Malgun Gothic, Vietnamese/English: Segoe UI; override with `--font`). It needs an ffmpeg build with libass (the `subtitles` filter) and libx264: `/record-video-skills:check-env` tells you. **Never ship a `.srt` with the same basename next to the `.mp4`** — players auto-load it as a second, giant subtitle layer.

## Guided tour — the default shape of every video

A manual video is a TOUR a user can repeat: the cursor walks every menu by hand, clicks every button, and the narration says what each click does. A tour that jumps between screens by URL and only points at values is hard to follow: viewers cannot tell where to click.

1. **No URL navigation in the visible part (after the first `MARK`):** never `page.goto` / `page.reload` / `#action=` to reach a screen. Use the real path with the cursor: `sidebar(app)` → `navbar(menu)` → `dropdown_item(item)` → button; `menu_path(page, cursor, ["Configuration", "Accounting & Tax", "Acme Bank accounts"])` does the common three (`app=False` when the path starts at the navbar). Click a button with `press_button(page, cursor, "Label")` (waits for the toast it raises; `wait_toast=False` when it navigates). Reload a list with `refresh_list` (list/kanban views only). A screen with no menu entry is not part of the tour — reach it through the button that opens it, or leave it out.
2. **URLs are allowed only in the cut lead-in:** login + visiting the heavy screens once (warm-up) before the first `MARK`, then `burn_subtitles.py --cut`. A lead-in with warm-up took 84-88 s on Odoo 17; give the first screen after login `wait_ready(..., timeout=90)`.
3. **Narration = one numbered instruction per click** ("Step N: click X in Y"): `mark_paced` BEFORE the helper call, so the subtitle is on screen while the cursor travels. The cue AFTER a click explains the FUNCTION of what opened and `point_at`s the key value. Template per language + measured pitfalls (numeric MARK sort, masking real values, credentials from environment variables): [REFERENCE.md](REFERENCE.md) section 9. ⛔ Never mask an `<input>` by writing its `.value`: Odoo saves it into the real record on the next button press — use `mask_input_overlay_js` + `assert_form_clean` (section 9).

## Core principles (details and reasons → [REFERENCE.md](REFERENCE.md))

- **Isolated environment:** a dedicated temporary profile and a headless browser of its own — never touch the session or profile the user is really using.
- **The cursor must mimic a real person:** easing (`ease_out_cubic`) between 2 points, then separate `mouse.down()/up()` — every click helper goes through this path, no exceptions, because this is what the viewer sees directly in the video.
- **Odoo dropdowns:** a native `<select>` → `.select_option()` directly; a custom OWL/React dropdown → `click_box()` to open it, then `click_text()` to pick the item (JS leaf-node search, case-insensitive, NBSP-normalized).
- **Reuse:** change nothing in `${CLAUDE_PLUGIN_ROOT}/scripts/` — write a new demo script (the UI steps + narration text) for the specific feature, importing `make_recorder` + the helpers. The pipeline is generic; do not rewrite it.
