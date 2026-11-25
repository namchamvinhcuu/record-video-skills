# record-demo-video — REFERENCE

Technical details + the reasons (WHY) for each component of the pipeline. Read the section matching the current work; no need to read it all.

## 0. Stealth driver (OPTIONAL, Linux only) — `${CLAUDE_PLUGIN_ROOT}/scripts/stealth_recorder.py`

> **Scope.** Not used on Windows or macOS (`make_recorder` refuses it there) and not what this plugin's workflow relies on: the plugin's default is the Chrome `DemoRecorder` + `capture="video"` path described in SKILL.md. This section documents an optional Linux driver. It runs a **third-party browser package** (`invisible-playwright-mcp`, a fingerprint-spoofing Firefox) fetched by `uvx` WITHOUT a pinned version, so an engine change can arrive silently (it did once: 0.45.0, 2026-11-04). Use it only on a machine you control, pin the version you tested, and never as an instruction to a Windows user.

`StealthRecorder(DemoRecorder)` reuses Xvfb, ffmpeg (same input-option order) and `mark()`, and replaces only the browser: Firefox through `invisible_playwright` (the engine of the `invisible-playwright-mcp` plugin; measured 0.40.0, Python 3.13):

```python
InvisiblePlaywright(seed=7, headless=False, show_cursor=True, locale="en-US",
                    extra_args=["--width=W", "--height=H", "--kiosk"],
                    pin={"screen.width": W, "screen.height": H, "screen.dpr": 1.0},
                    extra_prefs={"signon.rememberSignons": False})
browser.new_context(viewport={"width": W, "height": H}, screen={"width": W, "height": H})
```

- **Interpreter:** `uvx --from invisible-playwright-mcp python your_demo_script.py` resolves the plugin's environment without hardcoding its `~/.cache/uv/archive-v0/<hash>` path (the hash changes per machine and version). That environment vendors Playwright: `import playwright` fails there, so the Chrome fallback cannot run in it — `make_recorder(driver="auto")` picks stealth exactly when `invisible_playwright` is importable (Linux only — on macOS it always returns the Chrome `DemoRecorder`, SKILL.md §macOS).
- **Cursor:** `show_cursor=True` draws an arrow with a green click ripple inside the page; one `page.mouse.move(x, y)` is animated by the library (measured: 19 mousemove events over 0.57 s, ending on the target). The session cursor therefore has `native_motion=True` and `smooth_move` sends ONE move per target — splitting it into 30 eased steps would chain 30 humanized moves. `mouse.down()`/`up()` give 1 click. `mouse.wheel()` differs by size: a 120 px notch (what the helpers send) is 1 wheel event, as in Chrome, but ≥150 px becomes a burst of detents with the pointer creeping (a 360 px wheel = 4 detents + 2 moves; library `_scroll_plan`) — keep scrolling to the helpers. The session moves the real browser mouse to the CursorState centre before yielding, so the first wheel lands inside the page's scroller (without it, it went to (0,0) over the navbar and `bring_into_view` jumped 3380 px in one step — both drivers).
- **Login does not persist:** `StealthRecorder` starts Firefox with a fresh temporary profile every take (no `profile_dir`), unlike the Chrome path's persistent profile (section 1). Log in at the start of every take, before the first `MARK` — the lead-in cut removes it from the video.
- **Kiosk:** the frame is filled (inner = W x H measured; no 1-px edge, unlike Chrome) ONLY because `StealthRecorder` passes `pin={"screen.width": W, "screen.height": H, "screen.dpr": 1.0}`. The DPR matters too: `screen.width/height` are device pixels, a page reads CSS px = device px / DPR and the engine renders at the persona's DPR, so a DPR-1.25 seed (6, 20) gave inner 1024x576 (a 125 % zoomed page) while `page.viewport_size` — which `click_helpers.viewport_dims()` trusts — still said 1280x720. Since invisible-playwright 0.45.0 / engine build 20261104 (floated in by `uvx` on 2026-11-04), a `--kiosk` window takes the fingerprint persona's screen size (seed 7 → 1920x1080, seed 5 → 2560x1440) and ignores `new_context(viewport/screen)` and the Xvfb screen, so everything past the Xvfb edge silently left the video (measured 2026-11-06; 0.43.0/0.44.1 still followed the viewport). An early probe saw a window of ~2/3 of the screen with `--kiosk` alone, but with `new_context(viewport=…, screen=…)` as `StealthRecorder` uses it the capture is identical with or without `--width/--height` (measured 2026-10-21, 1280x720 and 1920x1080) — the flags are kept as a harmless belt-and-braces, not as the reason the frame is full.
- **Keyboard:** the stealth Firefox reads an uppercase `A` in a chord as Shift+A — `ControlOrMeta+A` selected nothing ("old" + "new" → "oldnew"); `type_into` presses `ControlOrMeta+a`.
- **Do not:** `locator.hover()` to show a value (parks the arrow ON it — `point_at`), `patch_chrome_prefs` (no-op here; the Firefox pref is passed at launch). Click through the helpers (move + down + hold + up) rather than `page.mouse.click(x, y)`: a real take found it might skip the walk; invisible_playwright 0.40.0's source wraps it to walk the pointer first (read, not measured), so this is about a visible press hold more than safety.
- **Evidence:** the launch recipe and the browser behaviour were proven by a full Odoo 17 manual take on a development database (2026-10-21, 3:48 raw, 26 MARKs: list/form/editable x2many/select_option, many2one + many2many_tags autocomplete, o-spreadsheet grid via wheel + move/down/up + keyboard, dialogs, breadcrumbs) — driven by that project's own script, NOT by `StealthRecorder` + `click_helpers`. The `StealthRecorder` + helpers path itself is verified on synthetic pages only (smoke run + probes: relocation, `type_into`, `point_at`, `scroll_through`, cleanup); the first real Odoo take through it is still to come. A take runs ~15% longer than with Chrome; MARKs stay right because they are timed from the ffmpeg start.
- **Fallback:** when the plugin or `uvx` is missing, run the Chrome path (sections 1-4) with an interpreter that has Playwright.

## 1. Môi trường quay cô lập (Xvfb + Playwright persistent context)

```bash
Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
```
`-nolisten tcp` turns off TCP X11 (local Unix-socket connections only) — the demo recording machine has no need to expose the display to the network.

**Kiosk window (default since 2026-10-21):** `DemoRecorder` launches Chrome with `--kiosk` next to `--window-position=0,0 --window-size=<W>,<H>`, so the video has no tab strip or address bar (before, every video showed them). Xvfb has no window manager to maximise the kiosk window — without `--window-size` it covers only ~2/3 of the screen. The page area becomes the full screen instead of ~992 px under the browser UI. `--window-size` is W+1,H+1 on purpose: Chrome's X window on Xvfb comes out 1 px short (W-1 x H-1, both modes), which left a 1-px black line on the right and bottom edge of every frame (measured 2026-10-21: 720/1280 dark px → 0 with +1; innerWidth/innerHeight then read W+1 x H+1, the extra pixel is off-screen); `click_helpers` reads innerHeight, so no script change is needed. `DemoRecorder(kiosk=False)` restores the normal window. The "Save password?" bubble still appears in kiosk — keep `patch_chrome_prefs` (item 5).

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir="/tmp/demo-profile-<slug>",   # KHÔNG dùng profile Chrome thật
        executable_path="/usr/bin/google-chrome",
        headless=False,                              # Xvfb đóng vai "màn hình thật"
        no_viewport=True,
        args=["--disable-notifications", "--disable-infobars",
              "--window-position=0,0", "--window-size=1921,1081", "--kiosk"],   # +1: see above
        ignore_default_args=["--no-sandbox", "--enable-automation"],
        env={**os.environ, "DISPLAY": ":99"},
    )
```

**Why `no_viewport=True` + `--window-position=0,0 --window-size=1920,1080`, not `viewport={...1920x1080}`:** with an emulated 1920x1080 viewport Playwright opens a 1928x1211 window at (10,10) on the 1920x1080 Xvfb screen — the page starts at screen row 97, so the bottom ~97 px of every page never reach x11grab (a button scrolled to the bottom edge was cut in half in the video), plus a 10 px black border top/left (measured 2026-10-19). With the window pinned to the screen, the whole page area is captured; it is `innerHeight` ≈ 992 px under the browser UI with `kiosk=False` (≈ full screen with the default kiosk), and `page.viewport_size` is then `None` — `click_helpers.viewport_dims()` reads `innerWidth/innerHeight` instead of assuming 1080.

**Why `ignore_default_args`:** Playwright adds `--no-sandbox` (and, in older versions, `--enable-automation`; Playwright 1.63 no longer does — ignoring it is harmless) to every `launch_persistent_context`; `--no-sandbox` puts a yellow "unsupported command-line flag" bar across the top of every frame, and `--disable-infobars` does NOT hide it (verified 2026-10-19, Chrome on Ubuntu, non-root, under Xvfb: with the two defaults ignored the bar is gone). Caveat: Chrome running as **root** refuses to start without `--no-sandbox` — record as a normal user.

**Why a dedicated `user_data_dir`, not the real profile:** (1) the real profile holds the user's history/bookmarks/extensions — easy to leak into the video by accident; (2) Chrome locks `SingletonLock` on the `user_data_dir` — opening 2 Chromes on the same profile (the real Chrome already running + the demo Chrome) crashes or refuses to start the second process.

**Why `launch_persistent_context` rather than `launch()` + `new_context()`:** we need the real Chrome (not Playwright's bundled Chromium) so the UI/fonts/rendering match exactly what the client's machine will use, and we need a profile that persists between runs (keeps the Odoo login cookie across recording retries).

## 2. Ghi hình (ffmpeg x11grab)

```bash
DISPLAY=:99 ffmpeg -f x11grab -video_size 1920x1080 -draw_mouse 0 -i :99.0 \
  -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p -y raw_capture.mp4
```

- ⛔ **`-draw_mouse 0` goes BEFORE `-i`.** It is an option of the x11grab input; placed after `-i` ffmpeg treats it as an output option and ignores it without failing the run. Until 2026-10-21 `recorder.py` and this snippet had it after `-i`, so every video carried the real X pointer at screen centre — an arrow, or a black hand whenever Chrome's hover state was over something clickable (Odoo list rows). Measured on the same scene: after `-i` 131 cursor pixels, before `-i` 0. Page CSS `cursor: none` does not help reliably: Chrome applies it only on the next mouse event, so after every navigation the pointer is drawn again until the script moves the mouse. Blurring it afterwards (`ffmpeg delogo`) is only a patch for an already-recorded take.

- `-draw_mouse 0`: Playwright's `page.mouse.move()` is a CDP input event — it never moves the real X pointer, which stays where Xvfb put it (screen centre) for the whole recording (verified 2026-10-19). With `-draw_mouse 1` that static arrow shows up next to the red dot whenever the page CSS cannot hide it (over the tab strip/address bar, during a page load), so it was never a real fallback. The red-dot overlay (item 4) is the only cursor in the video.
- The video timeline starts when ffmpeg is spawned: `DemoRecorder` stores that moment as `video_t0` and `rec.mark(step)` prints `MARK <seconds since video_t0> <step>` (item 7).
- `-preset medium` for the **raw capture** (not the final cut) — the final cut is re-encoded at the subtitle burn-in step, so the preset here does not decide the final quality; switch to `veryfast` on a weak recording machine, see the x264 preset documentation for the full speed/size tradeoff.
- Stop by sending `q` on stdin or `SIGINT` (graceful) — `kill -9`/`SIGKILL` cuts ffmpeg off while it is muxing the MP4 trailer → a broken file whose duration cannot be read.

## 3. Con trỏ chuột giả lập kiểu người — TẠI SAO không dùng `.click()` native

Playwright's `page.locator(...).click()` moves the mouse to the target **instantly** (no animation frame between the 2 points) and fires the click right away. In a video this immediately reads as "được điều khiển bằng script" (script-driven) — a client who has watched the 2 reference videos (recorded by hand by a person) will see the difference clearly in slow motion.

**Fix**: every click must route through a custom function that mimics real human movement — move along an easing curve, then a separate `mouse.down()`/`mouse.up()` (see `${CLAUDE_PLUGIN_ROOT}/scripts/click_helpers.py`):

```python
def ease_out_cubic(t: float) -> float:
    return 1 - (1 - t) ** 3

async def smooth_move(page, x0, y0, x1, y1, steps=30, duration=0.4):
    for i in range(1, steps + 1):
        t = ease_out_cubic(i / steps)
        await page.mouse.move(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        await asyncio.sleep(duration / steps)

# cursor: CursorState — theo dõi vị trí chuột giữa các lần gọi, để animate TỪ điểm trước
# ĐẾN đích mới thay vì luôn "animate" từ chính điểm đích (đứng yên). Snippet minh họa —
# xem `${CLAUDE_PLUGIN_ROOT}/scripts/click_helpers.py` cho implementation thật, KHÔNG copy trực tiếp đoạn này.
async def smooth_click(page, x, y, cursor, steps=30, duration=0.4, hold=0.05):
    await smooth_move(page, cursor.x, cursor.y, x, y, steps=steps, duration=duration)
    cursor.update(x, y)
    await page.mouse.down()
    await asyncio.sleep(hold)           # người thật giữ chuột 1 nhịp trước khi thả
    await page.mouse.up()
```

`ease_out_cubic` (fast at first, slowing down near the target) mimics a human hand better than linear — linear looks more "máy" (machine-like) even though it is animated.

**Scrolling (2026-10-19 — the viewer must see the whole page, and the camera must follow the action):**
- `click_box()`, `click_text()` and `type_into()` first bring the target into a clickable position (`scroll=True` by default, `bring_into_view()`) — scrolling DOWN when it is below the visible area, back UP when it is above (e.g. an input at the top of a long form after a button at the bottom).
- **"Visible" means the visible area of the target's scroller, not the viewport.** In Odoo 17 the `.o_content` scroller starts ≈104 px down (46 px navbar + ≈58 px control panel); aiming at a fixed margin from the viewport edge left small inputs clipped under the control panel and the click landed on the panel, silently. The helper intersects the viewport with every `overflow` ancestor that can clip the target and, once it has to scroll, keeps scrolling until the target sits inside a 40 px pad of that area (so it never stops flush against an edge; at the end of a page that cannot scroll further, a visible target is accepted as is).
- **`position: fixed` targets are clipped only by their containing block.** Odoo 17 dropdown poppers are `position: fixed` (`core/position_hook.js`) and rendered inside the field, e.g. inside the `.o_list_renderer.table-responsive` of an editable one2many line, whose `overflow-x: auto` makes `overflow-y` compute to `auto`. Counting that ancestor made a visible "add a product" item look clipped → a wheel → the AutoComplete closed → `RuntimeError`. Above a fixed element the walk skips every ancestor that is not a containing block (`transform`/`filter`/`perspective`/`contain`/`will-change`).
- **Targets inside an iframe** (Odoo html fields in Email Marketing, the Website editor): the iframe element is brought into view first, then the in-frame state is translated to main-page coordinates (`mouse.*` uses main-page coordinates; frame coordinates clicked the control panel). An in-frame hit-test is not enough: the Email Marketing iframe grows to its content (taller than the screen), so a target "visible inside the frame" can still be off-screen or under the control panel of the main page. The click point must hit through the whole frame chain (the target in its frame, then the `<iframe>` element in each parent); otherwise the helper does ONE `scroll_into_view_if_needed()` (Playwright scrolls every frame — a one-frame jump, not wheel notches) and checks again, then raises. Frame → main-page coordinates are mapped proportionally (`bounding_box()` size / in-frame rect size), so a CSS-scaled iframe (e.g. the Website "Add page" template previews) is clicked on the target, not offset by the scale factor onto a neighbour. Rotated/skewed frames are not handled. CSS `zoom` on or above an iframe → `RuntimeError` (Playwright 1.63's `bounding_box()` reports unzoomed coordinates; the frame-chain check rejects the point, so it fails safe) — use `scroll=False` there. A sticky header of the parent covering an in-iframe target → `RuntimeError` (safe, no click).
- **Never click blind:** the click point (the middle of the target's visible part — a field taller than the screen is not clicked on the frame edge) must hit-test (`document.elementFromPoint`) to the target or a descendant; still covered after scrolling → `RuntimeError`, never a click on whatever is on top.
- **Already visible and clickable → no wheel at all.** Navbar menus, control-panel buttons, breadcrumbs and an open dropdown's items are clicked directly: wheeling there only jitters the content behind, and Odoo 17's `AutoComplete` closes its dropdown on any scroll outside it.
- Scrolling uses `page.mouse.wheel()` notches of 120 px with a short pause — the page glides on video. The wheel scrolls the element UNDER the cursor, so before the first notch the cursor glides into the visible box of the target's scroller (a wheel over the navbar scrolls nothing). A notch that moves nothing → one `scrollIntoView({block: 'center'})` (a one-frame jump, last resort).
- `click_text()` keeps the node it found (an ElementHandle) for the scroll and the click; if that node disappears (a dropdown closed) it raises instead of re-searching the text, which could click another match elsewhere on the page.
- `type_into(..., clear=True)` presses select-all (`ControlOrMeta+a`: Ctrl+A on Linux, Cmd+A on macOS, where Ctrl+A only moves to the line start; Playwright ≥1.45; lowercase `a` because the stealth Firefox driver reads an uppercase `A` as Shift+A and selects nothing) before typing so the new text replaces the old one (`keyboard.type` only inserts: "Old name" + "New" → "Old nameNew"). In an html field / contenteditable (terms, signatures) select-all selects the WHOLE editor — pass `clear=False` to append instead.
- `scroll_through(page, cursor, back_to_top=False)`: shows a long page (a long list, a form with many tabs) by scrolling notch by notch until the scroll positions of ALL scrollers under the cursor (+ the window) stop changing, holding 1 s; `back_to_top=True` scrolls back up afterwards. Comparing all of them matters: with a nested scroller the wheel chains to the outer one and the inner position stops changing long before the end.
- `scroll_delta()` is the pure decision (0 = click now, negative = up, positive = down, `None` = scrolling cannot help) — unit-tested.
- **Tests:** `${CLAUDE_PLUGIN_ROOT}/scripts/tests/test_click_helpers_scroll.py` checks the Python logic against a fake page (fast, no browser); `${CLAUDE_PLUGIN_ROOT}/scripts/tests/test_click_helpers_real_chrome.py` runs the real helpers and their in-page JS in headless Chromium on the Odoo-17-faithful page shapes above (~9 s; prints `SKIP-FILE:` when Playwright or Chromium is missing). A change to the click/scroll code must keep both green — and the fake-page file alone cannot catch a JS bug. Neither covers `DemoRecorder`'s x11grab geometry: after a launch/geometry change, re-check a real take through `DemoRecorder.session()`.
- Verified 2026-10-19 end to end through `DemoRecorder.session()` (no_viewport geometry, Xvfb, ffmpeg) on Odoo-17-faithful pages: a button 3000 px down clicked and fully inside the captured frame; a 34 px input just under the control panel reached by scrolling up, clicked and its old text replaced; navbar/control-panel buttons with 0 wheel notches and no scroll change; a visible dropdown item picked with 0 notches — in a plain form, in an editable one2many line and in a dialog (fixed popper); an off-screen dropdown item → `RuntimeError` and no stray click; a button inside an iframe clicked at main-page coordinates — fixed-size, nested, cross-origin, `scale(0.5)`, scrolling inside its own document, and a 1600 px auto-height iframe with the target below the fold or under the control panel; a 1500 px field clicked in the middle of its visible part; an 850.5 px field (fractional, almost as tall as the visible area) clicked without a false error; `scroll_through` reached the bottom past a nested scroller.

## 4. Overlay con trỏ mũi tên + gợn sóng khi click (`${CLAUDE_PLUGIN_ROOT}/scripts/cursor_overlay.js`)

The overlay draws an SVG **arrow** whose **tip is the mouse position** (path point 0,0 sits at the pointer; the body hangs down-right like an OS cursor, scaled ×1.5 about the tip) and, on every real `mousedown`, a short blue ripple ring centred on the pointer that removes itself after ~0.5 s. `click_helpers.py` computes every target for the tip, never for the shape's bounding box. Both layers have `pointer-events: none`, so they never swallow a real click.

Inject it via `context.add_init_script()` **before opening the first page** — the script re-runs automatically on every new page/navigation in the context.

**Why use the real `mousemove` instead of drawing at coordinates the script computes:** Playwright's `page.mouse.move()` dispatches a **native trusted input event** — the DOM `mousemove` listener in `cursor_overlay.js` receives the event exactly as if a real mouse moved, so the arrow syncs tightly with `smooth_move` from item 3 without the 2 scripts sharing state.

**Why setup must be deferred by polling `document.documentElement`:** `add_init_script` runs at `document_start` — at that point `document.body`/`documentElement` may not exist yet (especially XML pages/early redirects), and assigning directly throws `TypeError: Cannot read properties of null`. Poll with a short `setInterval` until `document.documentElement` is ready, then `appendChild` the overlay div + set the CSS.

**Why the overlay still sets a global `cursor: none !important`:** it hides the X pointer inside the page for anyone watching the Xvfb display live (e.g. through VNC). It cannot hide the pointer outside the page (browser chrome) or before the CSS is loaded, which is why the recording itself uses `-draw_mouse 0` (item 2) instead of relying on it — a "double cursor" is easy to miss in a small preview and only shows clearly in full screen.

Full sample script: `${CLAUDE_PLUGIN_ROOT}/scripts/cursor_overlay.js`.

## 5. Tắt Chrome Translate popup — vì sao flag CLI không đủ

`--disable-features=Translate` or `--lang=vi` **does not reliably block** the Translate popup on every Chrome version (the behaviour changes between versions, not trustworthy). The reliable way: patch the `<user_data_dir>/Default/Preferences` file (JSON) directly **before Chrome starts with that profile for the first time**:

```python
prefs["translate_enabled"] = False
prefs.setdefault("intl", {})["accept_languages"] = "vi,vi-VN"
prefs["credentials_enable_service"] = False
prefs.setdefault("profile", {})["password_manager_enabled"] = False
```

The last two keys turn off Chrome's "Save password?" bubble: after the login step it shows the **username in clear text** at the top right of the page — a leak in a video delivered to a client (measured 2026-10-19 on Chrome 155 + Playwright 1.63; with these keys there is no bubble and no key icon).

`${CLAUDE_PLUGIN_ROOT}/scripts/patch_chrome_prefs.py` does this — run it **before** `launch_persistent_context()`. If Chrome already ran once with that profile before the patch, some keys get overwritten by Chrome on the next start — patching a **clean** (freshly created) profile is the safest.

## 6. Helper UI Odoo — native `<select>` vs custom dropdown OWL/React

- A native HTML `<select>` (an Odoo form-view `selection` field rendered as the old list-box) → `locator.select_option(value=...)` — Playwright fires the right `change` event itself, no smooth-click needed inside (the browser handles the native list-box UI itself; it is not DOM that Playwright can drive with the virtual cursor).
- A custom dropdown (OWL `Many2one`, React `<Select>`) is a `<div>` that renders its own list, not a real `<select>` → you must: `click_box()` to open the dropdown → `click_text()` to find the right item by text and click it.

`click_text()` (in `click_helpers.py`) is a **JS-based leaf-node text search**:
- Finds every **leaf** node (no element child) whose `textContent` matches (case-insensitive) — avoids wrongly matching a parent node that holds many child texts.
- An **exact** match wins over a substring match listed before it (since 2026-10-21): an Odoo many2one AutoComplete lists "Fabric Cutting (old)" above "Cutting", and the old first-substring rule picked the wrong record. Without an exact match the first substring match is used, as before — so pass the full visible label when two options share words. ⚠ "Exact" is page-wide: an exact leaf ANYWHERE (chatter tracking values, another one2many line, a breadcrumb — even off-screen) beats a partial match inside the open dropdown, and clicking it scrolls away and closes the AutoComplete. With a partial needle, make sure no other leaf on the page equals it.
- Normalizes whitespace: ` ` (NBSP, which Odoo often inserts between a number and its unit) → a plain space, then `trim()` + collapse consecutive spaces — comparing raw strings misses without normalization.
- Returns the matched NODE (an ElementHandle, kept across the scroll) → `bring_into_view` scrolls to it and hit-tests the middle of its visible part → `smooth_click` there (keeping the item 3 principle — never click directly with JS `.click()`).

**Odoo click/point gotchas:**
- **Pick a data cell with `td.o_data_cell:not(.o_handle_cell)`**, e.g. `click_box(page, row.locator("td.o_data_cell:not(.o_handle_cell)").first, cursor)`. In a sequence list the drag-handle cell carries `o_data_cell` too (`list_renderer.js`), so a bare `td.o_data_cell` `.first` is the handle, and clicking it starts a drag instead of opening the record.
- **Point at a cell, not a row**, when the narration talks about one value: `point_at(page, row.locator("td[name=qty]"), cursor)`. `point_at` on an element wider than half the viewport (a full-width list row, a wide dialog) parks beside the VISIBLE part of the first text inside it: the text box is clipped to the viewport and to every `overflow` ancestor, because Odoo 17 truncates every list cell (`text-truncate`) and the unclipped text would put the pointer next to another column's value. Beside a full-width row itself, the pointer would fall on the screen's left edge and point at nothing.
- **Pointing at the statusbar pops a large tooltip** over the form — point at the field the stage explains, or keep the hold short.

**Waiting for an Odoo page — never `networkidle`:** the Odoo web client keeps its bus websocket/longpoll open for the whole session, so `page.wait_for_load_state("networkidle")` never fires (timed out after 30 s on Odoo 17). Use `wait_ready(page, selector)` from `click_helpers.py`: `domcontentloaded` + wait until a specific selector is visible + a short settle pause (default 0.5 s) for OWL rendering/animations. Typical selectors: `.o_list_view .o_data_row` (list loaded), `.o_form_view` (form opened), `.modal .modal-body` (dialog shown), `.o_login_form` (login page). Navigate with `page.goto(url, wait_until="domcontentloaded")` too — the default `load` is fine, but never pass `networkidle`.

## 7. Phụ đề song ngữ — pipeline MARK-log → .srt → burn-in

1. **While the demo runs**, each step calls `rec.mark("<step_name>")`, which prints `MARK <elapsed_seconds> <step_name>` to the log (`elapsed` counted from when ffmpeg starts recording, NOT from when the script starts — in a real Odoo 17 run the two differed by 10.49 s: Xvfb + Chrome + the first page load. Timing from the script start shifted every subtitle and left ~9 s of loading screen at the start).
   - **ffmpeg logs to its own file** `<out_path>.ffmpeg.log` (look there when the capture is empty or ffmpeg fails). In one log shared with stdout, its `\r` progress lines glued onto MARK lines and a shell `grep "^MARK"` missed some; `build_srt.py` itself always read them (it splits on `\r` too) and now also finds `MARK <seconds> <step>` after a logger prefix, so prefer it over grep.
   - **Pace the marks for reading:** marks fire as fast as the UI allows, so many cues lasted 2-4 s, too short to read. Call `rec.pace_from(narration)` once (the same `{step: {lang: text}}` dict as below) and use `await rec.mark_paced("<step>")` instead of `rec.mark(...)` (without `pace_from` it raises RuntimeError rather than silently recording unpaced): it first waits until the PREVIOUS step has been on screen `cue_seconds(text)` = max(3 s, characters / 16) for its longest language (one take serves every language), and leaving `session()` normally waits out the last cue. `build_srt.py` gives the last cue the same `cue_seconds` length. Plain `mark()` is unchanged.
2. **Choose the lead-in cut** `<cut>`: about a second before the first `MARK` — everything before it is the loading screen.
3. `${CLAUDE_PLUGIN_ROOT}/scripts/build_srt.py` reads the log + the narration file (`{step_name: {"vi": "...", "ko": "..."}}`) → builds the `.srt` with `--offset = -(cut) - 0.3`: `-(cut)` moves the cues onto the cut video's timeline, and the **empirically tuned** `-0.3` makes the narration appear JUST BEFORE the action, not right after, so the client can read it before seeing the UI change.
4. Burn-in with `${CLAUDE_PLUGIN_ROOT}/scripts/burn_subtitles.py --input trimmed.mp4 --lang <lang> --cut <cut>` — **the default deliverable, approved 2026-10-16**. It runs:
   ```bash
   ffmpeg -ss <cut> -i trimmed.mp4 -vf "pad=1920:1240:0:0:black,subtitles=out_vi.srt:force_style='FontName=Noto Sans CJK KR,FontSize=36,PlayResX=1920,PlayResY=1240,Alignment=2,MarginV=55,Outline=0,Shadow=0,BorderStyle=1'" \
     -c:v libx264 -preset veryfast -crf 20 -pix_fmt yuv420p -movflags +faststart final_vi.mp4
   ```
   - **One video per language** — never stacked bilingual lines.
   - **The subtitles live in a black band padded BELOW the capture** (`pad`, default band 160 px), so they never cover the UI. `Noto Sans CJK KR` covers both Vietnamese and Korean glyphs.
   - **PlayResX/PlayResY MUST match the real canvas — after the pad, `PlayResY` = 1080 + band** (1240), not 1080 — libass assumes a 384×288 canvas by default when they are not set and scales the font completely out of proportion (verified empirically — missing these 2 fields makes the subtitle text abnormally large or misplaced).
   - **`-ss` goes BEFORE `-i`**: the filter then sees timestamps starting at 0 after the cut, matching the `-(cut)` SRT offset. With `-ss` after `-i` the filter sees the original timestamps and the cues land in the cut-away part (verified 2026-10-19 on a testsrc clip: the cue vanished).
   - **No same-basename `.srt` next to the delivered `.mp4`** — players auto-load it and draw a second, giant subtitle layer. `burn_subtitles.py` refuses `--srt` = the mp4's sidecar name and warns if one is lying next to the output.
   - Before using the `subtitles=` filter, probe for it first — this filter depends on a build-time libass, some minimal ffmpeg builds (e.g. Homebrew core) lack it, and the error shows up as a parse error pointing at the wrong cause rather than saying "thiếu libass" (libass missing).

## 8. Trim đoạn đen cuối video

After recording stops, the last few seconds are usually a black screen (tab closed/focus moved). `${CLAUDE_PLUGIN_ROOT}/scripts/trim_black_tail.py`:
1. Uses `PIL` to sample the last N frames (via `ffmpeg -ss ... -frames:v 1 -f image2pipe`, or a quick extract with `ffmpeg` + reading the bytes) and computes each frame's mean brightness.
2. Finds the **first moment counting from the end** where brightness drops below the threshold (`--threshold`, empirical default ~10/255) and **stays there continuously** to the end of the video (avoids cutting at a single black frame flashing mid-video, e.g. during a page change).
3. Trim: `ffmpeg -i raw.mp4 -t <cutoff> -c copy out.mp4` (stream copy is enough because the cut is at the END, no frame accuracy needed between two segments — unlike a multi-cut concat).

## 9. Guided tour — narration template and measured pitfalls

Rule and rationale: SKILL.md §Guided tour (the first customer demo, rejected as hard to follow: URL jumps, values pointed at but no clicks shown). Everything below was measured in real takes (2026-11-18, Odoo 17, stealth recorder, 22-33 MARKs per take, VI + KO) and reported to this skill; it is not re-measured here. ⚠ Verification limit: in those takes `sidebar`/`navbar`/`dropdown_item`/`refresh_list` and the toast-aware `press_button` ran as the script's OWN copies with the same selectors and timings as the helpers in `click_helpers.py` (20+ uses per take); a direct call of the skill's helpers (`menu_path`, `press_button`, `tour_helpers.py` included) in a real take is not recorded yet. Now `sidebar`/`navbar`/`dropdown_item`/`press_button` all match the whole label case-insensitively (`exact_text_re`); that is verified on twin pages and in real headless Chromium, not on a real Odoo.

**Script shape — the MARK comes BEFORE the action** (the subtitle is on screen while the cursor travels), the cue after a click explains what opened:

```python
await rec.mark_paced("click_app");  await sidebar(page, cursor, "Configuration")
await rec.mark_paced("click_menu"); await navbar(page, cursor, "Accounting & Tax")
await rec.mark_paced("click_item"); await dropdown_item(page, cursor, "Acme Bank accounts")
await wait_ready(page, ".o_list_table", timeout=45)
await rec.mark_paced("view_function")
await point_at(page, page.locator(".o_data_row td", has_text="1,000.00").first, cursor, hold=2.5)
```

**Narration dict — one numbered instruction per click, then the FUNCTION of the screen** (write every target language; `<…>` are placeholders):

```python
NARRATION = {
    "home":          {"vi": "Đây là màn hình chính. Chúng ta sẽ đi từng bước bằng các menu thật.",
                      "en": "This is the home screen. We will go step by step through the real menus.",
                      "ko": "메인 화면입니다. 실제 메뉴를 따라 단계별로 이동해 보겠습니다."},
    "click_app":     {"vi": "Bước 1: ở thanh bên trái, bấm vào ứng dụng <App>.",
                      "en": "Step 1: in the left sidebar, click the <App> app.",
                      "ko": "1단계: 왼쪽 사이드바에서 <App> 앱을 클릭합니다."},
    "click_menu":    {"vi": "Bước 2: trên thanh menu phía trên, bấm vào <Menu>.",
                      "en": "Step 2: in the top menu, click <Menu>.",
                      "ko": "2단계: 상단 메뉴에서 \"<Menu>\" 메뉴를 클릭합니다."},
    "click_item":    {"vi": "Bước 3: trong nhóm <Group>, chọn <Item>.",
                      "en": "Step 3: in the <Group> group, choose <Item>.",
                      "ko": "3단계: <Group> 그룹에서 \"<Item>\" 항목을 선택합니다."},
    "view_function": {"vi": "Đây là <màn hình>. Cột <X> cho biết <ý nghĩa / chức năng>.",
                      "en": "This is <screen>. The <X> column shows <meaning / function>.",
                      "ko": "이 화면은 <화면>입니다. <X> 열은 <의미/기능>을 나타냅니다."},
}
```
A value that changes during the take (a balance read before and after a live action) goes in as `{b0}` / `{b1}` placeholders filled from the real read, per language (`1.000.000` for vi, `1,000,000` for en/ko). `tour_helpers.py` does both fillings, `{S}` step numbers and values:

```python
from tour_helpers import fill_narration, format_number
NARRATION = fill_narration(NARRATION_TEMPLATE, {          # {S} -> 1, 2, 3 ... counted per step that has it
    "b0": {l: format_number(balance_before, l) for l in ("vi", "en", "ko")},
})                                                         # a {word} left unfilled raises ValueError
json.dump(NARRATION, open("narration.json", "w"), ensure_ascii=False)   # the file build_srt.py reads
```

**Pitfalls (each one cost a take):**
- **Match button/menu text case-INsensitively** (Odoo 17, 2026-11-18): the DOM text is "Test call" but the theme shows "Test Call" (CSS `text-transform`), so `get_by_role("button", name="Test Call", exact=True)` matched nothing and the take died after a 30 s timeout (about 2.5 min of recording plus the diagnosis lost). Click a button with `press_button(page, cursor, "Test Call")` (it matches with `exact_text_re`: whole label, case-insensitive; `scope=` defaults to `.o_form_view`). When a selector finds nothing, probe the DOM (`outerHTML`) before blaming timing.
- **Auto step numbers:** write `{S}` in the narration text ("Step {S}: click …") and let `fill_narration` replace it, so adding a scene never means renumbering by hand (worked in a 33-mark take, as the script's own code).
- **After a button that raises a toast** (Fetch Accounts): `press_button(page, cursor, "Fetch Accounts")` waits for the previous `.o_notification` to disappear (15 s, a stuck one is only reported on stderr), clicks, waits for the new one (90 s, raises if it never comes), then holds `TOAST_SETTLE` = 4 s so the subtitle covers it.
- **A button that NAVIGATES** (a smart button such as Transactions) needs no toast wait: `press_button(page, cursor, "Transactions", wait_toast=False)`, then `wait_ready(page, ".o_list_table")`; only a button that raises a toast needs the wait above.
- **`refresh_list` only works on a list or kanban view** (it clicks `.o_searchview_input`, which a form does not have) and has no wait of its own beyond its 1.5 s settle: follow it with `wait_ready` when the view is heavy.
- **Opening an app lands on its DEFAULT first menu** (Configuration opened an "Industries" list): say so in the step 1 cue and let step 2 continue from there, otherwise viewers wonder why a random list appears.
- **Never hard-code the OUTCOME of a click in narration** ("nothing new was reported", amounts): it differs on a fresh account. Fill it from a live read (`{b0}`/`{b1}`) or keep the cue generic. Put the result cue BEFORE the post-click sleep (or merge it into the button cue): the toast was already gone at the `call_result` mark although the cue said it "appears as a notification".
- **Korean particles:** `<Item>를/을` depends on how the name ends, so a template with the particle glued to a placeholder is wrong half the time; use `"<Item>" 항목을 선택합니다` (name in quotes + a fixed noun). Keep every cue 4-10 s (measured 3.7-10.4 s; the two-sentence 10.4 s cue was the heaviest to read), and say once in the first cue that each "Step N" is one click the viewer can repeat.
- **Sort the MARK log NUMERICALLY** when you merge or dedupe it: `sort -k2,2 -n -u`. A text sort put `105.52` before `88.41` and shifted every subtitle after it.
- **A stealth take that dies in the login step with a blank page** = the web client is still loading, not a broken script: `wait_ready(page, ".o_main_navbar", timeout=60..90)` for the first screen. The whole lead-in (login + warm-up of ~8 screens + one reconcile click + a 20 s wait) took 84-88 s before the first scene: visit the heavy screens in the lead-in, before the first `MARK`, and cut with `--cut`. It is the lead-in length, not a cold-start constant.
- **Hide sensitive values IN the video** (bank account numbers, personal data) with `await context.add_init_script(script=mask_text_js(r"\b5500\d{4}(\d{4})\b", "********$1"))` (`tour_helpers.py`; before the first page, like the cursor overlay): it rewrites matching text nodes every 100 ms, so Odoo's redraws are re-masked (it masked 12-digit account numbers in a real take, as the script's own copy). The pattern must fit the real number length (a 16-digit account needs `\d{8}`), a number split over several `<span>`s does not match, and text typed in an `<input>` is not a text node, so it is NOT masked. And **filter lists** (search box) instead of scrolling past real rows.
- ⛔ **NEVER mask an `<input>` / `<textarea>` by writing its `.value`** (real take, 2026-11-19): Odoo SAVES the changed value on the next button press or navigation. A script that rewrote an account-number input saved `6600****0003` over `660000000003` in a real record (repaired by hand afterwards). `mask_text_js` only rewrites text nodes, so it is safe. For an input use `mask_input_overlay_js(pattern, replacement)` (`tour_helpers.py`): it draws an OVERLAY element over each matching `<input>`/`<textarea>` (default `interval_ms=30`, follows scroll/resize) and never writes `.value`; and call `await assert_form_clean(page)` before a button press or navigation so a take aborts when a form has unsaved changes (a NEW record counts as dirty in Odoo 17). `mask_text_js` now also installs a MutationObserver (`observe=True`, default) so a node Odoo adds or rewrites is masked before the next paint, without the old 100 ms flash; the interval stays as a safety net. A replacement that matches the pattern again (`a` → `aa`) is bounded but multiplies on the first parse: keep the replacement from matching.
- **`press_button` with a toast raised by an ERROR**: a `Validation Error` / server-error dialog (`.o_error_dialog`) never gives a toast, so the helper now raises `RuntimeError("... an error dialog appeared instead of a toast: '<text>'")` at once instead of hanging 90 s. Known gap: when an OLD toast is stuck the wait returns immediately and a dialog that appears 300 ms later is missed.
- **A label with a count** ("Reconcile 17 Items"): read the label from the DOM first (`inner_text` of the button), never hard-code it, or a re-take breaks when the count changes.
- **`wait_ready` with a selector whose FIRST match is hidden** (`.o_kanban_record`) timed out: use `:has-text(...)` or a more specific selector. Bootstrap `d-flex !important` ignores `style.display = 'none'`: use `style.setProperty('display', 'none', 'important')` to hide a card. `click_box` on `.o_facet_remove` raised `hit False` (hit-test fails): press Backspace in the search box instead.
- **`point_at` on a wide input parks far from a short value**: aim at the value with `scroll_into_view` + `smooth_move` yourself (one take wrote its own `pv()` for it).
- **Korean cues of 3 lines do not fit the default subtitle band** (160 px, font 36): `burn_subtitles.py --band 220 --font-size 34 --margin-v 40` kept them inside.
- **A proxy such as Cloudflare refuses Python's default `User-Agent`** (`Python-urllib/3.x` → HTTP 403, error code 1010) on helper calls made during a take (e.g. reading a balance by JSON-RPC to quote it in the narration): send an explicit `User-Agent` header.
- **Credentials:** read the login and password from environment variables in the demo script (`ODOO_DEMO_LOGIN`, `ODOO_DEMO_PASSWORD`); never type them into the chat and never print them. Use ONLY a throwaway demo account on a demo system (the variables live in the environment of the terminal that started Claude Code, so everything Claude runs there can read them). `demo_template.py` implements this: https only (plain http for localhost), no login inside the URL, the password and Playwright's debug switches (`DEBUG`, `PWDEBUG`, …) removed from its own environment before any browser/ffmpeg process starts, an overlay over every password field, the password typed only into a page on the `ODOO_DEMO_URL` host, and the temporary browser profile (it holds the demo session) deleted at the end. The raw capture still shows the login frames and a failed conversion leaves its webm in a `rdv-video-*` temp folder: delete raw captures and any leftover `rdv-video-*` folder once the final video is made.

## Script templates trong `${CLAUDE_PLUGIN_ROOT}/scripts/`

| File | Role | Run or read? |
|---|---|---|
| `stealth_recorder.py` | `StealthRecorder` (default driver, Firefox stealth, section 0) + `make_recorder(driver="auto"|"stealth"|"chrome")` + `stealth_available()` | Import into your demo script; run it with `uvx --from invisible-playwright-mcp python` |
| `recorder.py` | `DemoRecorder` (Chrome fallback): starts Xvfb, the ffmpeg subprocess, `launch_persistent_context`; `mark()` timed from the video start; on macOS `capture="video"` = Playwright `record_video_dir` + webm→mp4 (SKILL.md §macOS) | Import into your demo script |
| `cursor_overlay.js` | JS injected via `add_init_script` | Point at the path; do not edit unless changing the arrow colour/size or the ripple (keep the tip at path point 0,0; `tests/test_cursor_overlay_real_chrome.py` pins it) |
| `click_helpers.py` | `smooth_click`, `click_box`, `click_text`, `type_into`, guided-tour helpers `sidebar` / `navbar` / `dropdown_item` / `menu_path` / `refresh_list` / `press_button` (section 9), `point_at` / `point_beside` (show a value without covering it; in the main page it also works on `pointer-events: none` targets such as Odoo's current statusbar stage — inside an iframe such a target raises, use `scroll=False` there), `bring_into_view`, `scroll_into_view`, `scroll_through`, `viewport_dims`, `ease_out_cubic`, `wait_ready` | Import — every click, keystroke, scroll and page wait goes through here |
| `tour_helpers.py` | `fill_narration` (`{S}` step numbers + per-language values), `format_number`, `mask_text_js` (init script that masks sensitive text nodes), `mask_input_overlay_js` (overlay for `<input>`/`<textarea>`, never writes `.value`), `assert_form_clean` (aborts on a dirty form), no browser needed to import (section 9) | Import into your demo script |
| `patch_chrome_prefs.py` | Patches the `Preferences` JSON to turn off Translate and the "Save password?" bubble | Run once before launching the context |
| `build_srt.py` | MARK log + narration → `.srt` | Run after recording, before burn-in |
| `burn_subtitles.py` | Padded band + burned subtitles + lead-in cut, one language per run | Run per language after `build_srt.py` |
| `trim_black_tail.py` | Finds + cuts the black tail | Run after stopping ffmpeg (Linux capture only: the Windows/macOS video path ends in a frozen frame, not black) |
| `check_env.py` | Prerequisite check with the exact install command per missing item | `/record-video-skills:check-env` before the first take |
| `fonts_probe.py` | Font choice per subtitle language (Windows) + libass/libx264 probe of the installed ffmpeg | Imported by `burn_subtitles.py` and `check_env.py` |
| `text_io.py` | UTF-8 stdout/stderr, encoding-tolerant text reading (UTF-8, UTF-16) | Imported by every command-line script |

## 10. macOS notes (why the video path, not a screen grab)

`ffmpeg -f avfoundation` / `screencapture` record the user's REAL screen (every window and notification — the opposite of the isolation this skill promises) and need the Screen Recording permission of the *responsible app* (the terminal app that launched Claude Code, not the Claude sandbox). Measured on macOS 27.0.1 (2026-11-18): `could not create image from display` until the app was toggled ON in System Settings > Privacy & Security > Screen & System Audio Recording; the prompt has no Allow button, so reset the permission with `tccutil reset ScreenCapture <bundle-id>`, reopen the app, toggle it by hand, "Quit & Reopen". If a real-screen capture is ever needed, preflight `screencapture -x <png>` (rc 0 + non-empty file) and print these steps instead of recording. `ffmpeg -f avfoundation` itself was never run — unverified. The Playwright video path needs none of this.
