"""Helper of test_stealth_x11grab_real.py — NOT a test file (runs under the plugin interpreter).

Usage: uvx --from invisible-playwright-mcp python stealth_scene.py <scripts_dir> <display> <out_dir> [WxH]

Records one scene through make_recorder() → StealthRecorder.session() on a REAL Xvfb + ffmpeg +
stealth Firefox and prints ONE JSON line (prefix `SCENE_JSON `) with what the test asserts on.
The display must be free; the wrapped start_xvfb() checks that OUR Xvfb owns its lock file.

Env: RDV_SCENE_SEED=<n> → make_recorder(seed=n) (default: the recorder's own seed);
RDV_SCENE_MODE=geometry → a short scene instead of the full one: a fixed black square in the page's
bottom-right corner, then inner/screen/devicePixelRatio/page.viewport_size/versions (the
kiosk window must follow the Xvfb size at DPR 1, not the fingerprint persona's screen or DPR);
RDV_SCENE_HANG=1 → see main().
"""
import asyncio
import importlib.metadata as md
import json
import os
import sys
import time

SCRIPTS, DISPLAY, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, SCRIPTS)
from click_helpers import click_box, point_at, scroll_through, type_into  # noqa: E402
from stealth_recorder import StealthRecorder, make_recorder  # noqa: E402

W, H = (int(v) for v in (sys.argv[4] if len(sys.argv) > 4 else "1280x720").split("x"))
HTML = ("<html><head><style>html,body{margin:0;background:#eef;font:20px sans-serif}"
        "#b{position:absolute;left:300px;top:200px;padding:10px 16px}"
        "#v{position:absolute;left:900px;top:450px}"
        "#i{position:absolute;left:300px;top:600px;width:200px}</style></head><body>"
        "<button id=b>Save</button><span id=v>677.5</span><input id=i value=old>"
        "<script>window.__c=[];document.addEventListener('click',e=>__c.push(e.target.id),true)</script>"
        "</body></html>")
# Odoo-17-like: fixed navbar + control panel over the ONLY scroller (.o_content from 104 px).
ODOO_FAR = ("<html><head><style>body{margin:0;font:16px sans-serif}</style></head><body>"
            "<div style='position:fixed;top:0;left:0;right:0;height:46px;background:#714B67'></div>"
            "<div style='position:fixed;top:46px;left:0;right:0;height:58px;background:#ddd'></div>"
            "<div id=oc style='position:absolute;top:104px;bottom:0;left:0;right:0;overflow:auto'>"
            "<button id=nav style='position:fixed;top:8px;left:20px;height:30px;z-index:5'>MENU</button>"
            "<div style='height:3000px'></div><button id=far0 style='height:40px'>FAR</button>"
            "<div style='height:2500px'></div><button id=far1 style='height:40px'>FAR1</button>"
            "<div style='height:600px'></div></div>"
            "<script>window.__w=[];window.__s=[];window.__c=[];"
            "addEventListener('wheel',e=>__w.push([e.clientX,e.clientY]),{capture:true});"
            "document.getElementById('oc').addEventListener('scroll',e=>__s.push(e.target.scrollTop));"
            "addEventListener('click',e=>__c.push(e.target.id),{capture:true});</script></body></html>")
# Odoo-17-like full-width list row: icon handle cell (::before glyph, no text node) + checkbox + an empty
# wide cell, so the first text sits mid-row.
WIDE_ROW = ("<html><head><style>body{margin:0;font:16px sans-serif;background:#fff}"
            "table{width:100%;border-collapse:collapse;table-layout:fixed;margin-top:300px}"
            "td{height:32px;padding:0 8px;white-space:nowrap}.fa-sort::before{content:'\\2195'}</style></head><body>"
            "<table><tbody>\n<tr class=o_data_row id=row>\n"
            "<td style='width:40px'><span class='fa fa-sort'></span></td>\n"
            "<td style='width:40px'><input type=checkbox></td>\n<td style='width:500px'></td>\n"
            "<td><span id=t>QC/2026/0001</span></td>\n<td>Inspection lot</td>\n</tr></tbody></table>"
            "<script>window.__c=[];document.addEventListener('click',e=>__c.push(e.target.id),true)</script>"
            "</body></html>")
TRUNC_ROW = ("<html><head><style>body{margin:0;font:16px sans-serif;background:#fff}"
             "table{width:100%;border-collapse:collapse;table-layout:fixed;margin-top:300px}"
             "td{height:32px;padding:0 8px}</style></head><body><table><tbody><tr class=o_data_row id=row>"
             "<td style='width:40px'><input type=checkbox></td>"
             "<td id=cell style='width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'><span id=t>"
             "[FURN_8888] Large Cabinet with a very long display name that Odoo cuts with an ellipsis in lists</span></td>"
             "<td style='width:100px'>5.00</td><td id=state>Draft</td></tr></tbody></table></body></html>")
ADD_FAR = ("() => { const s=document.createElement('div'); s.style.height='2400px'; document.body.appendChild(s);"
           " const b=document.createElement('button'); b.id='far'; b.textContent='Far';"
           " b.style.cssText='position:absolute;left:300px;top:1800px;padding:10px 16px';"
           " document.body.appendChild(b); }")
CORNER = 48     # geometry mode: side of the fixed black square in the page's bottom-right corner
CORNER_PAGE = ("<html><head><style>html,body{margin:0;background:#eef}"
               "#br{position:fixed;right:0;bottom:0;width:%dpx;height:%dpx;background:#000}</style></head>"
               "<body><div id=br></div></body></html>" % (CORNER, CORNER))
SCREEN_JS = "[screen.width, screen.height]"


async def versions(page) -> dict:
    """Resolved package + engine versions (a future red is attributable at a glance)."""
    out = {}
    for pkg in ("invisible-playwright", "invisible-core", "invisible-playwright-mcp"):
        try:
            out[pkg] = md.version(pkg)
        except md.PackageNotFoundError:
            out[pkg] = None
    try:
        out["browser"] = page.context.browser.version
    except Exception as e:          # diagnosis only — never fails the scene
        out["browser"] = "unknown (%s)" % type(e).__name__
    out["ua"] = await page.evaluate("navigator.userAgent")
    return out


def persona(seed):
    """The fingerprint persona's UNPINNED screen panel [w, h] (device px) and DPR for `seed`, through the
    PUBLIC invisible_core export. An error string in both fields when that is not available."""
    try:
        from invisible_core import generate_profile
        p = generate_profile(seed)
        return [p.screen.width, p.screen.height], p.screen.dpr
    except Exception as e:          # premise data only; the test names the failure
        why = "unknown (%s: %s)" % (type(e).__name__, e)
        return why, why


def checked_recorder(info: dict):
    seed = os.environ.get("RDV_SCENE_SEED")
    kw = {"seed": int(seed)} if seed else {}
    rec = make_recorder(DISPLAY, os.path.join(OUT, "raw.mp4"), "", resolution="%dx%d" % (W, H),
                        xvfb_startup_wait=0, **kw)
    info["recorder"] = type(rec).__name__
    info["seed"] = getattr(rec, "seed", None)
    assert isinstance(rec, StealthRecorder), info
    orig = rec.start_xvfb

    def checked_start_xvfb():
        orig()
        sock = "/tmp/.X11-unix/X" + DISPLAY[1:]
        for _ in range(100):
            if rec._xvfb_proc.poll() is not None:
                raise RuntimeError("Xvfb %s exited rc=%s" % (DISPLAY, rec._xvfb_proc.returncode))
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("Xvfb %s socket never appeared" % DISPLAY)
        with open("/tmp/.X%s-lock" % DISPLAY[1:]) as f:
            if int(f.read().strip()) != rec._xvfb_proc.pid:
                raise RuntimeError("display %s is owned by another X server" % DISPLAY)

    rec.start_xvfb = checked_start_xvfb
    return rec


def report(rec, info: dict, t0: float):
    info["display_after"] = os.environ.get("DISPLAY")
    info["ffmpeg_log"] = rec.ffmpeg_log
    info["ffmpeg_log_size"] = os.path.getsize(rec.ffmpeg_log) if os.path.exists(rec.ffmpeg_log) else -1
    info["ffmpeg_log_fh_after"] = rec._ffmpeg_log_fh is not None
    info["scene_secs"] = round(time.monotonic() - t0, 2)
    print("SCENE_JSON " + json.dumps(info), flush=True)


async def geometry():
    info = {"display_before": os.environ.get("DISPLAY")}
    rec = checked_recorder(info)
    t0 = time.monotonic()
    async with rec.session() as (page, cursor):
        await page.set_content(CORNER_PAGE)
        await page.wait_for_selector("#br")
        await asyncio.sleep(0.3)
        info["t_corner"] = rec.video_elapsed()
        info["inner"] = await page.evaluate("[innerWidth, innerHeight]")
        info["screen"] = await page.evaluate(SCREEN_JS)
        info["dpr"] = await page.evaluate("devicePixelRatio")
        vs = page.viewport_size                        # what click_helpers.viewport_dims() trusts
        info["viewport_size"] = [vs["width"], vs["height"]] if vs else None
        info["corner_box"] = await page.locator("#br").bounding_box()
        info["versions"] = await versions(page)
        info["persona_screen"], info["persona_dpr"] = persona(rec.seed)
        await asyncio.sleep(1.5)                       # frames for the corner check
        rec.mark("scene_end")
    report(rec, info, t0)


async def main():
    info = {"display_before": os.environ.get("DISPLAY")}
    rec = checked_recorder(info)
    t0 = time.monotonic()
    async with rec.session() as (page, cursor):
        if os.environ.get("RDV_SCENE_HANG"):           # timeout-cleanup test: hang with everything running
            print("SCENE_HANGING pid=%d" % os.getpid(), flush=True)
            await asyncio.sleep(3600)
        info["display_inside"] = os.environ.get("DISPLAY")
        info["cursor_start"] = [cursor.x, cursor.y, cursor.native_motion]
        # Count OUR page.mouse.move calls per helper action: native motion = one call per target
        # (the library animates it). 30-step easing would give 30 per target.
        mouse = page.mouse
        info["mouse_object_stable"] = page.mouse is mouse
        moves = []
        orig_move = mouse.move

        async def counting_move(x, y, *a, **kw):
            moves.append((x, y))
            return await orig_move(x, y, *a, **kw)

        mouse.move = counting_move
        counts = info["move_calls"] = {}

        async def act(name, coro):
            n0 = len(moves)
            await coro
            counts[name] = len(moves) - n0

        # FIRST action of the session: a far target, no mouse move before it.
        await page.set_content(ODOO_FAR)
        await act("click_box_far_first", click_box(page, page.locator("#far0"), cursor))
        info["first"] = await page.evaluate("() => ({wheels: __w, scrolls: __s, clicks: __c})")
        await act("click_box_navbar", click_box(page, page.locator("#nav"), cursor))
        # cursor over the navbar → bring_into_view relocates into the scroller, then clicks
        await act("click_box_far_relocated", click_box(page, page.locator("#far1"), cursor))
        info["odoo_clicks"] = await page.evaluate("__c")
        await act("scroll_through", scroll_through(page, cursor, pause=0.02, hold=0))
        await page.set_content(HTML)
        await page.wait_for_selector("#i")
        await act("click_box_visible", click_box(page, page.locator("#b"), cursor))
        await act("point_at", point_at(page, page.locator("#v"), cursor, hold=0))
        info["t_point"] = rec.video_elapsed()
        info["point"] = [cursor.x, cursor.y]
        info["v_box"] = await page.locator("#v").bounding_box()
        info["clicks_after_point"] = await page.evaluate("__c")
        await asyncio.sleep(1.5)                       # frames for the capture checks
        await act("type_into", type_into(page, page.locator("#i"), "new", cursor))
        info["input"] = await page.evaluate("document.getElementById('i').value")
        await page.evaluate(ADD_FAR)
        await act("click_box_far_window", click_box(page, page.locator("#far"), cursor))
        info["clicks"] = await page.evaluate("__c")
        info["scroll_y"] = await page.evaluate("scrollY")
        info["far_box"] = await page.locator("#far").bounding_box()
        info["inner"] = await page.evaluate("[innerWidth, innerHeight]")
        info["screen"] = await page.evaluate(SCREEN_JS)
        info["versions"] = await versions(page)
        # point_at on a full-width list row → beside its first text, not x = 8.
        # Called directly (not through act()): move_calls stays the per-helper table above.
        await page.set_content(WIDE_ROW)
        await page.wait_for_selector("#t")
        await point_at(page, page.locator("#row"), cursor, hold=0)
        info["wide_point"] = [cursor.x, cursor.y]
        info["wide_text_box"] = await page.locator("#t").bounding_box()
        info["wide_row_box"] = await page.locator("#row").bounding_box()
        info["wide_clicks"] = await page.evaluate("__c")
        # An Odoo text-truncate first cell — beside the VISIBLE cell end, not the hidden overflow.
        await page.set_content(TRUNC_ROW)
        await page.wait_for_selector("#t")
        await point_at(page, page.locator("#row"), cursor, hold=0)
        info["trunc_point"] = [cursor.x, cursor.y]
        info["trunc_cell_box"] = await page.locator("#cell").bounding_box()
        info["trunc_text_box"] = await page.locator("#t").bounding_box()
        info["trunc_state_box"] = await page.locator("#state").bounding_box()
        rec.mark("scene_end")
    report(rec, info, t0)


asyncio.run(geometry() if os.environ.get("RDV_SCENE_MODE") == "geometry" else main())
