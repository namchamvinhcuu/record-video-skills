"""Click helpers cho record-demo-video — mọi thao tác click trong demo script PHẢI đi qua
đây, không gọi `.click()` native của Playwright. Native `.click()` teleport con trỏ tức
thời (không có frame chuyển động nào) — trong video demo, thứ này lộ ngay là bị điều
khiển bằng script khi khách tua chậm. Xem REFERENCE.md §3.

Dùng với Playwright ASYNC API (async_playwright / launch_persistent_context async).
"""
from __future__ import annotations

import asyncio
import re
import sys


def ease_out_cubic(t: float) -> float:
    """Nhanh lúc đầu, chậm dần lúc gần đích — mô phỏng tay người tốt hơn linear."""
    return 1 - (1 - t) ** 3


async def smooth_move(page, x0: float, y0: float, x1: float, y1: float,
                       steps: int = 30, duration: float = 0.4, native: bool = False) -> None:
    """Di chuyển chuột từ (x0,y0) tới (x1,y1) qua `steps` frame, easing ease-out-cubic.
    native=True (trình duyệt tự vẽ chuyển động kiểu người — invisible_playwright): MỘT
    mouse.move tới đích, thư viện tự sinh đường cong (đo: 1 lệnh → 19 mousemove / 0.57 s);
    chia 30 bước ở đây sẽ thành 30 cú di chuyển người-giả nối nhau, chậm và giật."""
    if native:
        await page.mouse.move(x1, y1)
        return
    for i in range(1, steps + 1):
        t = ease_out_cubic(i / steps)
        await page.mouse.move(x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        await asyncio.sleep(duration / steps)


class CursorState:
    """Theo dõi vị trí chuột hiện tại giữa các lần gọi smooth_click — cần để animate
    TỪ vị trí trước ĐẾN đích mới, thay vì luôn animate từ chính điểm đích (đứng yên)."""

    def __init__(self, x: float = 0, y: float = 0, native_motion: bool = False):
        self.x, self.y = x, y
        # True khi driver tự vẽ con trỏ + chuyển động (StealthRecorder): mọi helper đi một
        # mouse.move/đích thay vì tự easing (smooth_move native=True).
        self.native_motion = native_motion

    def update(self, x: float, y: float) -> None:
        self.x, self.y = x, y


async def smooth_click(page, x: float, y: float, cursor: CursorState,
                        steps: int = 30, duration: float = 0.4,
                        hold: float = 0.05) -> None:
    """Di chuyển mượt tới (x, y) rồi mouse.down()/up() tách rời (người thật giữ chuột
    một nhịp trước khi thả, không nhấn-thả tức thời)."""
    await smooth_move(page, cursor.x, cursor.y, x, y, steps=steps, duration=duration,
                      native=cursor.native_motion)
    cursor.update(x, y)
    await page.mouse.down()
    await asyncio.sleep(hold)
    await page.mouse.up()


SCROLL_PAD = 40        # px chừa trong vùng nhìn thấy khi PHẢI cuộn — không dính sát mép
SCROLL_STEP = 120      # px mỗi nấc con lăn (≈ 1 notch chuột thật)
SCROLL_PAUSE = 0.05    # giây giữa 2 nấc — đủ chậm để người xem theo kịp


def scroll_delta(top: float, bottom: float, vis_top: float, vis_bottom: float,
                 hit: bool = True, pad: float = SCROLL_PAD,
                 step: float = SCROLL_STEP, settle: bool = False) -> float | None:
    """Quyết định nấc cuộn KẾ TIẾP cho phần tử [top, bottom] (toạ độ viewport) trong vùng
    nhìn thấy [vis_top, vis_bottom] của khung cuộn chứa nó (giao viewport với mọi
    ancestor có overflow — Odoo 17: `.o_content` bắt đầu ≈104 px, dưới navbar + control
    panel, nên "khung" KHÔNG phải viewport).

    `hit` = điểm click (tâm, kẹp vào vùng nhìn thấy) đúng là phần tử, không bị che.
    Trả về 0 = click được ngay; số âm = cuộn LÊN, dương = cuộn XUỐNG (kẹp ±step);
    None = cuộn không giúp được (phần tử đã ở giữa vùng mà vẫn bị che).

    Phần tử ĐÃ thấy trọn + hit → 0, kể cả sát mép (menu navbar, nút control panel, item
    dropdown đang mở): cuộn lúc đó chỉ làm rung trang và đóng dropdown OWL.
    settle=True (đã cuộn ít nhất 1 nấc): chỉ dừng khi phần tử vào hẳn vùng đệm `pad` —
    không để phần tử vừa cuộn tới dừng sát mép khung (cách đáy 8 px, dính dưới panel)."""
    region = vis_bottom - vis_top
    height = bottom - top
    edge = min(pad, max(0.0, (region - height) / 2)) if settle else 0.0
    if hit and (height > region or (top >= vis_top + edge and bottom <= vis_bottom - edge)):
        return 0.0
    # Vùng đích của mép trên khi phải cuộn: chừa `pad`; phần tử cao hơn vùng → phủ kín.
    a, b = vis_top + pad, vis_bottom - pad - height
    lo, hi = min(a, b), max(a, b)
    if top < lo:
        need = top - lo
    elif top > hi:
        need = top - hi
    else:
        if hit:
            # Thấy + click được, chỉ lệch <1 px so với chỗ duy nhất settle chấp nhận (phần tử
            # cao gần bằng vùng, toạ độ lẻ .5 mà scroll nhảy theo px nguyên) → nhận, KHÔNG None
            # (None = báo lỗi trên phần tử click được).
            return 0.0
        # Đã ở vùng đích mà vẫn bị che (header sticky…) → kéo về giữa vùng nhìn thấy.
        need = (top + bottom) / 2 - (vis_top + vis_bottom) / 2
        if abs(need) < 1:
            return None
    return max(-step, min(step, need))


# JS (chạy trên ElementHandle): trạng thái để quyết định cuộn + điểm click an toàn.
# vis* = giao viewport với mọi ancestor CẮT được phần tử (overflow khác visible); scroller =
# ancestor cuộn được gần nhất (hoặc null = window). Phần tử `position: fixed` (popper
# dropdown Odoo 17 — position_hook.js đặt fixed, render NGAY trong field) không bị cắt bởi
# ancestor nào trừ containing block của nó (transform/filter/perspective/contain/will-change)
# → bỏ qua các ancestor đó, nếu không item dropdown trong dòng one2many (`.table-responsive`
# có overflow-y auto) bị coi là khuất → lăn chuột → AutoComplete đóng.
# Điểm click = giữa PHẦN NHÌN THẤY của phần tử (phần tử cao hơn màn → không click ở mép);
# hit = elementFromPoint tại đó là chính phần tử hoặc con của nó (không bị panel che).
# pointing=true (point_at, không click): nhận cả TỔ TIÊN của phần tử — phần tử
# `pointer-events: none` (stage hiện tại của statusbar Odoo 17 = `.btn:disabled`) không bao giờ
# là elementFromPoint, trả về cha nó; panel đè lên (không chứa phần tử) vẫn tính là che.
# rLeft/top là toạ độ TRONG frame của phần tử — bring_into_view đổi sang trang chính.
_TARGET_STATE_JS = """
(el, pointing) => {
  if (!el || !el.isConnected) return null;
  const r = el.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return null;
  let top = 0, bottom = window.innerHeight, left = 0, right = window.innerWidth;
  let scroller = null;
  const cb = (st) => st.transform !== 'none' || st.filter !== 'none' || st.perspective !== 'none'
                     || /(paint|layout|strict|content)/.test(st.contain) || /transform|filter/.test(st.willChange);
  let fixedBelow = getComputedStyle(el).position === 'fixed';
  for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
    const st = getComputedStyle(a);
    if (fixedBelow) { if (!cb(st)) continue; fixedBelow = false; }
    if (st.position === 'fixed') fixedBelow = true;
    if (st.overflowY !== 'visible') {
      const ar = a.getBoundingClientRect();
      top = Math.max(top, ar.top); bottom = Math.min(bottom, ar.bottom);
      left = Math.max(left, ar.left); right = Math.min(right, ar.right);
      if (!scroller && /(auto|scroll)/.test(st.overflowY) && a.scrollHeight > a.clientHeight) scroller = a;
    }
  }
  const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
  // Giữa phần nhìn thấy; phần tử nằm ngoài vùng hẳn → tâm kẹp vào mép (hit sẽ false).
  const mid = (a0, a1, lo, hi) => {
    const s = Math.max(a0, lo), e = Math.min(a1, hi);
    return e > s ? (s + e) / 2 : clamp((a0 + a1) / 2, lo + 1, hi - 1);
  };
  const x = mid(r.left, r.right, left, right);
  const y = mid(r.top, r.bottom, top, bottom);
  const h = document.elementFromPoint(x, y);
  const sb = scroller ? scroller.getBoundingClientRect() : null;
  return {
    top: r.top, bottom: r.bottom, rLeft: r.left, rWidth: r.width, rHeight: r.height,
    visTop: top, visBottom: bottom, x, y,
    hit: !!h && (h === el || el.contains(h) || (!!pointing && h.contains(el))),
    box: sb ? [Math.max(sb.left, 0), Math.max(sb.top, 0),
               Math.min(sb.right, window.innerWidth), Math.min(sb.bottom, window.innerHeight)]
            : [0, 0, window.innerWidth, window.innerHeight],
  };
}
"""


# JS: điểm (x, y) TRONG frame của el có nằm trong viewport frame đó và trúng el (hoặc con)?
_HIT_AT_JS = """
(el, [x, y]) => {
  if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
  const h = document.elementFromPoint(x, y);
  return !!h && (h === el || el.contains(h));
}
"""


async def _hits_chain(handle, x: float, y: float) -> bool:
    """Điểm (x, y) toạ độ TRANG CHÍNH trúng handle qua cả chuỗi frame: trong frame của nó
    trúng chính nó, và ở mỗi frame cha trúng thẻ iframe chứa nó (không ra ngoài màn,
    không bị control panel của trang cha che)."""
    h = handle
    while True:
        bb = await h.bounding_box()
        if bb is None:
            return False
        lx, ly, lw, lh = await h.evaluate(
            "(el) => { const r = el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; }")
        # Quy đổi ngược trang chính → frame theo TỈ LỆ (iframe bị transform: scale → bb đã scale).
        fx = lx + (x - bb["x"]) * (lw / bb["width"] if bb["width"] else 1.0)
        fy = ly + (y - bb["y"]) * (lh / bb["height"] if bb["height"] else 1.0)
        if not await h.evaluate(_HIT_AT_JS, [fx, fy]):
            return False
        frame = await h.owner_frame()
        if frame is None or frame.parent_frame is None:
            return True
        h = await frame.frame_element()


async def viewport_dims(page) -> tuple[float, float]:
    """(width, height) vùng trang. recorder.py launch với no_viewport=True nên
    page.viewport_size = None → đọc innerWidth/innerHeight thật, KHÔNG giả định 1080."""
    vs = page.viewport_size
    if vs:
        return vs["width"], vs["height"]
    w, h = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
    return w, h


async def bring_into_view(page, handle, cursor: CursorState, max_steps: int = 200,
                          pause: float = SCROLL_PAUSE, pointing: bool = False) -> tuple[float, float]:
    """Cuộn bằng con lăn chuột thật (mouse.wheel → trang trôi mượt trong video) tới khi
    phần tử click được; trả về điểm click (x, y) ĐÃ hit-test. Không bao giờ click mù.

    - mouse.wheel cuộn phần tử NẰM DƯỚI con trỏ → trước nấc đầu, dời con trỏ (mượt) vào
      vùng nhìn thấy của khung cuộn CHỨA phần tử (item dropdown → cuộn trong menu, không
      cuộn `.o_content` làm AutoComplete OWL tự đóng).
    - Một nấc không nhúc nhích: phần tử đã thấy + hit (vd dòng cuối trang, không còn chỗ
      cuộn vào vùng đệm) → nhận luôn; chưa → scrollIntoView({block:'center'}) một lần.
    - Phần tử biến mất (dropdown đóng) / vẫn không click được → RuntimeError.
    - Phần tử trong iframe (html field Email Marketing, Website editor): đưa chính thẻ
      iframe vào khung trước, rồi đổi toạ độ trong frame sang toạ độ trang chính (mouse.*
      của Playwright dùng toạ độ trang chính — click toạ độ frame = click nhầm panel).
      Hit-test trong frame KHÔNG đủ: iframe Email Marketing tự giãn theo nội dung (cao hơn
      màn hình) nên "thấy trong frame" vẫn có thể nằm ngoài màn / dưới control panel ở
      trang chính → điểm click phải trúng qua CẢ chuỗi frame
      (_hits_chain); trượt → scroll_into_view_if_needed một lần (Playwright cuộn mọi frame,
      giật 1 nhịp nhưng đúng) rồi kiểm lại; vẫn trượt → RuntimeError."""
    frame = await handle.owner_frame()
    in_frame = frame is not None and frame.parent_frame is not None
    if in_frame:
        frame_el = await frame.frame_element()
        await bring_into_view(page, frame_el, cursor, max_steps=max_steps, pause=pause, pointing=pointing)
    jumped = False

    async def accept(st):
        """Điểm click đã kiểm ở trang chính, hoặc None (iframe cao: chưa click được)."""
        if not in_frame or await _hits_chain(handle, st["x"], st["y"]):
            return st["x"], st["y"]
        return None

    async def read_state():
        st = await handle.evaluate(_TARGET_STATE_JS, pointing)
        if st is None or frame is None or frame.parent_frame is None:
            return st
        bb = await handle.bounding_box()
        if bb is None:
            return None
        # Frame → trang chính theo TỈ LỆ bb/rect, không cộng offset hằng: iframe bị CSS
        # scale (vd preview "Add page" của Website) thì offset hằng click lệch đúng hệ số
        # scale, trúng phần tử khác mà _hits_chain không bắt được.
        sx = bb["width"] / st["rWidth"] if st["rWidth"] else 1.0
        sy = bb["height"] / st["rHeight"] if st["rHeight"] else 1.0
        x0, y0 = st["rLeft"], st["top"]
        st["x"], st["y"] = bb["x"] + (st["x"] - x0) * sx, bb["y"] + (st["y"] - y0) * sy
        l, t, r, b = st["box"]
        st["box"] = [bb["x"] + (l - x0) * sx, bb["y"] + (t - y0) * sy,
                     bb["x"] + (r - x0) * sx, bb["y"] + (b - y0) * sy]
        return st

    state = None
    forced = scrolled = False
    for _ in range(max_steps):
        state = await read_state()
        if state is None:
            raise RuntimeError("Phần tử đã biến mất hoặc bị ẩn (dropdown tự đóng? chưa mount?) — kiểm tra selector/text")
        dy = scroll_delta(state["top"], state["bottom"], state["visTop"], state["visBottom"], state["hit"],
                          settle=scrolled)
        if dy == 0:
            point = await accept(state)
            if point is not None:
                return point
            if jumped:
                break
            await handle.scroll_into_view_if_needed()
            jumped = True
            continue
        if dy is None:
            break
        left, top, right, bottom = state["box"]
        if not (left <= cursor.x <= right and top <= cursor.y <= bottom):
            tx, ty = (left + right) / 2, (top + bottom) / 2
            await smooth_move(page, cursor.x, cursor.y, tx, ty, native=cursor.native_motion)
            cursor.update(tx, ty)
        await page.mouse.wheel(0, dy)
        scrolled = True
        await asyncio.sleep(pause)
        after = await read_state()
        if after is not None and abs(after["top"] - state["top"]) < 1:
            if scroll_delta(after["top"], after["bottom"], after["visTop"], after["visBottom"], after["hit"]) == 0:
                point = await accept(after)
                if point is not None:
                    return point
            if forced:
                state = after
                break
            await handle.evaluate("(el) => el.scrollIntoView({block: 'center'})")
            forced = True
    raise RuntimeError("Không đưa được phần tử vào vị trí click được (bị che hoặc không cuộn tới) — "
                       f"trạng thái cuối: {state}")


async def _locator_handle(locator):
    handle = await locator.element_handle()
    if handle is None:
        raise RuntimeError("Locator không khớp phần tử nào — kiểm tra selector")
    return handle


async def scroll_into_view(page, locator, cursor: CursorState, **kw) -> tuple[float, float]:
    """Cuộn mượt (lên hoặc xuống) tới khi locator click được; trả về điểm click."""
    return await bring_into_view(page, await _locator_handle(locator), cursor, **kw)


async def click_box(page, locator, cursor: CursorState, scroll: bool = True, **kw) -> None:
    """Click vào phần tử của một Playwright locator, qua smooth_click. Mặc định cuộn mượt
    tới phần tử trước (ở dưới → cuộn xuống, ở trên → cuộn lên) và hit-test điểm click.
    scroll=False → click tâm bounding box như cũ, không cuộn, không hit-test."""
    if scroll:
        cx, cy = await scroll_into_view(page, locator, cursor)
    else:
        box = await locator.bounding_box()
        if box is None:
            raise RuntimeError("Locator không có bounding box (ẩn hoặc chưa mount?) — kiểm tra selector")
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await smooth_click(page, cx, cy, cursor, **kw)


POINT_GAP = 16   # px từ mép phần tử tới ĐẦU mũi tên overlay (đầu mũi = toạ độ chuột; thân vẽ xuống-phải) — đầu mũi không đè lên chữ


def point_beside(box: dict, view_width: float, gap: float = POINT_GAP) -> tuple[float, float]:
    """Điểm đặt con trỏ CẠNH phần tử, không đè lên nó: bên phải mép phải + gap, giữa chiều
    cao; bên phải không đủ chỗ (sát mép màn hình) → bên trái mép trái − gap."""
    y = box["y"] + box["height"] / 2
    right = box["x"] + box["width"] + gap
    if right <= view_width - gap / 2:
        return right, y
    return max(gap / 2, box["x"] - gap), y


# Phần NHÌN THẤY của chữ đầu tiên bên trong phần tử, tính LỆCH so với khung phần tử (hệ toạ độ của
# frame chứa nó) + kích thước khung phần tử (ew/eh) để point_at đổi sang toạ độ trang theo tỉ lệ
# bounding_box()/ew — iframe bị scale CSS thì cộng thẳng lệch sẽ sai (đo ở
# scale 0.75: lệch ~97 px). Range rect bị CẮT theo viewport và mọi tổ tiên overflow != visible: Odoo 17
# cắt chữ mọi ô list (text-truncate, list_renderer.scss:80) — rect chưa cắt rộng cả câu, con trỏ đứng
# cạnh giá trị CỘT KHÁC (đo: 534 px qua mép ô, cả Chrome lẫn Firefox stealth). Không còn phần nhìn
# thấy (chỉ icon/ô trống/cuộn ngang khuất) → null.
_FIRST_TEXT_BOX_JS = """
(el) => {
  const er = el.getBoundingClientRect();
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim()) continue;
    const st = getComputedStyle(n.parentElement);
    if (st.visibility !== 'visible' || st.display === 'none') continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    const r = range.getBoundingClientRect();
    let l = Math.max(r.left, 0), t = Math.max(r.top, 0);
    let rt = Math.min(r.right, innerWidth), b = Math.min(r.bottom, innerHeight);
    for (let a = n.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      const s = getComputedStyle(a);
      if (s.overflowX !== 'visible' || s.overflowY !== 'visible') {
        const q = a.getBoundingClientRect();
        l = Math.max(l, q.left); t = Math.max(t, q.top);
        rt = Math.min(rt, q.right); b = Math.min(b, q.bottom);
      }
    }
    if (rt - l > 0 && b - t > 0)
      return {dx: l - er.left, dy: t - er.top, width: rt - l, height: b - t, ew: er.width, eh: er.height};
  }
  return null;
}
"""


async def point_at(page, locator, cursor: CursorState, hold: float = 1.5,
                   gap: float = POINT_GAP, scroll: bool = True, **kw) -> None:
    """Chỉ cho người xem MỘT giá trị (ô số, badge, nút) mà KHÔNG che nó: cuộn tới phần tử
    như click_box, dời con trỏ mượt tới CẠNH phải phần tử (point_beside) rồi đứng yên `hold`
    giây — không click. Đặt con trỏ vào tâm phần tử che đúng con số lời thuyết minh đang
    nói ("677.5"). Cuộn ở chế độ pointing: nhận phần tử
    `pointer-events: none` (stage statusbar, nút disabled) thay vì báo "bị che".
    Phần tử rộng hơn nửa màn hình (dòng list `.o_data_row`, dialog rộng): trỏ cạnh phần NHÌN THẤY
    của chữ đầu tiên bên trong nó — với dòng full-width, cạnh chính phần tử rơi về mép trái màn
    hình, không trỏ vào gì. Không có chữ nhìn thấy → như cũ."""
    if scroll:
        await scroll_into_view(page, locator, cursor, pointing=True)
    box = await locator.bounding_box()
    if box is None:
        raise RuntimeError("Locator không có bounding box (ẩn hoặc chưa mount?) — kiểm tra selector")
    width, _ = await viewport_dims(page)
    if box["width"] > width / 2:
        tb = await locator.evaluate(_FIRST_TEXT_BOX_JS)
        if tb:
            sx = box["width"] / tb["ew"] if tb.get("ew") else 1.0
            sy = box["height"] / tb["eh"] if tb.get("eh") else 1.0
            box = {"x": box["x"] + tb["dx"] * sx, "y": box["y"] + tb["dy"] * sy,
                   "width": tb["width"] * sx, "height": tb["height"] * sy}
    x, y = point_beside(box, width, gap)
    await smooth_move(page, cursor.x, cursor.y, x, y, native=cursor.native_motion, **kw)
    cursor.update(x, y)
    await asyncio.sleep(hold)


async def type_into(page, locator, text: str, cursor: CursorState,
                    delay: float = 0.06, clear: bool = True, **kw) -> None:
    """Cuộn tới ô nhập (kể cả khi nó nằm PHÍA TRÊN vùng đang xem), smooth-click vào nó
    rồi gõ từng phím như người thật (delay giây/phím) — không fill() tức thời.
    clear=True: Ctrl/Cmd+A (ControlOrMeta) trước khi gõ để thay nội dung cũ (khác fill(), keyboard.type chỉ
    chèn tại con trỏ: "Old name" + "New" → "Old nameNew"). Html field/contenteditable
    (điều khoản, chữ ký…): select-all thay CẢ editor → dùng clear=False để gõ thêm vào."""
    await click_box(page, locator, cursor, **kw)
    if clear:
        # ControlOrMeta: Cmd+A trên macOS (Ctrl+A ở đó chỉ về đầu dòng → chữ mới chèn TRƯỚC chữ cũ), Ctrl+A nơi khác.
        # Chữ "a" THƯỜNG: Firefox stealth (invisible_playwright) hiểu "A" hoa là Shift+A → không chọn hết,
        # "old" + "new" = "oldnew" (đo 2026-10-21); Chrome nhận cả hai.
        await page.keyboard.press("ControlOrMeta+a")
        if not text:
            await page.keyboard.press("Delete")
    await page.keyboard.type(text, delay=delay * 1000)


# JS: vị trí cuộn của MỌI container cuộn được chứa điểm (x, y) + window.scrollY. Odoo
# cuộn `.o_content`, không phải window; đọc mỗi ancestor gần nhất sẽ dừng sớm khi con lăn
# chuyển sang cuộn container ngoài (scroll chaining) — trang còn ~2600 px chưa chiếu.
_SCROLL_POS_JS = """
([x, y]) => {
  const pos = [];
  let el = document.elementFromPoint(x, y);
  while (el && el !== document.documentElement) {
    const st = getComputedStyle(el);
    if (/(auto|scroll)/.test(st.overflowY) && el.scrollHeight > el.clientHeight) pos.push(el.scrollTop);
    el = el.parentElement;
  }
  pos.push(window.scrollY);
  return pos;
}
"""


async def scroll_through(page, cursor: CursorState, back_to_top: bool = False,
                         step: float = SCROLL_STEP, pause: float = 0.12,
                         hold: float = 1.0, max_steps: int = 400) -> None:
    """Cho người xem thấy HẾT trang dài: dời con trỏ vào giữa khung, lăn xuống từng nấc
    tới đáy (vị trí cuộn không đổi nữa), dừng `hold` giây; back_to_top=True thì lăn
    ngược lên đầu (vd trước khi nhập ô ở trên cùng)."""
    width, height = await viewport_dims(page)
    cx, cy = width / 2, height / 2
    await smooth_move(page, cursor.x, cursor.y, cx, cy, native=cursor.native_motion)
    cursor.update(cx, cy)
    directions = [step] + ([-step] if back_to_top else [])
    for dy in directions:
        for _ in range(max_steps):
            before = await page.evaluate(_SCROLL_POS_JS, [cx, cy])
            await page.mouse.wheel(0, dy)
            await asyncio.sleep(pause)
            if await page.evaluate(_SCROLL_POS_JS, [cx, cy]) == before:
                break
        await asyncio.sleep(hold)


async def wait_ready(page, selector: str, timeout: float = 15.0, settle: float = 0.5) -> None:
    """Chờ trang "sẵn sàng để quay" thay cho `wait_for_load_state("networkidle")`.

    Odoo web client giữ websocket/longpoll bus mở suốt phiên nên networkidle KHÔNG bao
    giờ tới (đo thật: timeout 30 s trên Odoo 17). Thay bằng: domcontentloaded + chờ một
    selector cụ thể hiện ra (vd `.o_list_view .o_data_row`, `.o_form_view`, `.modal`)
    + nghỉ ngắn `settle` giây cho animation/OWL render xong trước khi click tiếp."""
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_selector(selector, state="visible", timeout=timeout * 1000)
    if settle > 0:
        await asyncio.sleep(settle)


_NBSP = " "

# JS chạy trong trang: tìm node LÁ (không có element-child) mà textContent khớp,
# chuẩn hoá NBSP + whitespace trước khi so sánh — Odoo hay chèn NBSP giữa số/đơn vị.
# Khớp ĐÚNG text thắng khớp chứa-chuỗi đứng trước nó: AutoComplete many2one liệt kê
# "Fabric Cutting (old)" trước "Cutting" → trả node chứa-chuỗi đầu tiên là chọn nhầm
# Không có node khớp đúng → node chứa-chuỗi đầu tiên như cũ.
_FIND_LEAF_TEXT_JS = """
(args) => {
  const [needle, caseSensitive] = args;
  const norm = (s) => s.replace(/\\u00A0/g, ' ').trim().replace(/\\s+/g, ' ');
  const target = caseSensitive ? norm(needle) : norm(needle).toLowerCase();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let node, partial = null;
  while ((node = walker.nextNode())) {
    if (node.children.length > 0) continue;   // chỉ xét node lá
    const text = norm(node.textContent || '');
    const cmp = caseSensitive ? text : text.toLowerCase();
    const exact = cmp === target;
    if (exact || (!partial && target && cmp.includes(target))) {
      const r = node.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) {
        if (exact) return node;   // trả NODE (handle) — cuộn/click bám đúng node này
        partial = node;
      }
    }
  }
  return partial;
}
"""


async def click_text(page, text: str, cursor: CursorState,
                      case_sensitive: bool = False, timeout: float = 5.0,
                      scroll: bool = True, **kw) -> None:
    """Tìm node lá có text khớp (case-insensitive theo mặc định, chuẩn hoá NBSP) rồi
    smooth-click vào tâm bounding box của nó. Dùng cho dropdown custom OWL/React —
    item không phải <option> nên không select_option() được. Mặc định cuộn mượt tới
    node trước khi click (node nằm ngoài khung hình → toạ độ click rơi ra ngoài).

    Giữ ĐÚNG node tìm được lần đầu (ElementHandle): sau khi cuộn KHÔNG tìm lại theo
    text — dropdown đã đóng thì một node khác cùng chữ ở chỗ khác sẽ bị click nhầm;
    node biến mất → RuntimeError (bring_into_view), không click toạ độ cũ."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        handle = await page.evaluate_handle(_FIND_LEAF_TEXT_JS, [text, case_sensitive])
        node = handle.as_element()
        if node is not None:
            if scroll:
                x, y = await bring_into_view(page, node, cursor)
            else:
                box = await node.bounding_box()
                if box is None:
                    raise RuntimeError(f"Node chứa text '{text}' không còn bounding box")
                x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            await smooth_click(page, x, y, cursor, **kw)
            return
        await handle.dispose()
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Không tìm thấy text '{text}' sau {timeout}s")
        await asyncio.sleep(0.1)


# ---- Guided-tour menu helpers (Odoo web client) -------------------------------------------
# A demo/manual video must be a GUIDED TOUR: every screen is reached with the cursor through the
# real path — sidebar app → top navbar menu → dropdown item → button — never page.goto(url).
# Viewers who watch a screen appear by URL cannot tell where to click (the first customer demo was
# rejected as "hard to follow"). Each helper pauses MENU_PAUSE first so the viewer sees the mouse arrive
# at the target, then goes through click_box (human motion + hit-test), as everything else does.

MENU_PAUSE = 0.8        # s before each menu click: the mouse is visible on its way
SIDEBAR_SETTLE = 2.5    # s after opening an app: its first view loads (lazy assets)
NAVBAR_SETTLE = 1.0     # s after a navbar click: the dropdown renders
TOAST_SETTLE = 4.0      # s a notification stays in view after a button raised it
ERROR_DIALOG = ".o_error_dialog"   # contentClass of every Odoo 17 error dialog (core/errors/error_dialogs.xml)


def exact_text_re(name: str) -> "re.Pattern[str]":
    """^name$ with surrounding whitespace allowed: a sidebar link must match its WHOLE label
    ("Account" must not click "Accounting" / "Account Reports"). Case-insensitive: the theme may
    show "Test Call" (CSS text-transform) for a DOM text of "Test call"."""
    return re.compile(r"^\s*%s\s*$" % re.escape(name), re.IGNORECASE)


async def sidebar(page, cursor: CursorState, name: str,
                  pause: float = MENU_PAUSE, settle: float = SIDEBAR_SETTLE) -> None:
    """Click an app in the visible left sidebar (Odoo 17 app switcher), exact label match."""
    link = page.locator("a:visible").filter(has_text=exact_text_re(name)).first
    await asyncio.sleep(pause)
    await click_box(page, link, cursor)
    await asyncio.sleep(settle)


async def navbar(page, cursor: CursorState, name: str,
                 pause: float = MENU_PAUSE, settle: float = NAVBAR_SETTLE) -> None:
    """Click a menu in the top navbar (`.o_main_navbar`), whole label, case-insensitive. A menu
    with children opens its dropdown (next: dropdown_item); a plain entry (e.g. Dashboard)
    opens its view."""
    target = page.locator(".o_main_navbar").get_by_text(exact_text_re(name)).first
    await asyncio.sleep(pause)
    await click_box(page, target, cursor)
    await asyncio.sleep(settle)


async def dropdown_item(page, cursor: CursorState, name: str,
                        pause: float = MENU_PAUSE, settle: float = 0.0) -> None:
    """Click an item of the OPEN dropdown (`.dropdown-menu:visible`), whole label,
    case-insensitive. The caller waits for the opened view (wait_ready) — the load time depends
    on the screen."""
    target = page.locator(".dropdown-menu:visible").get_by_text(exact_text_re(name)).first
    await asyncio.sleep(pause)
    await click_box(page, target, cursor)
    if settle:
        await asyncio.sleep(settle)


async def menu_path(page, cursor: CursorState, path: list[str], app: bool = True) -> None:
    """Walk a real menu path with the cursor. app=True: path[0] is a sidebar app, the rest is
    navbar [+ dropdown item]; app=False: the path starts at the navbar. After the app part the
    rest is `[navbar]` (a plain navbar entry) or `[navbar, dropdown item]`. Anything else raises
    ValueError — a deeper path is written call by call so every click can carry its own MARK."""
    rest = list(path[1:] if app else path)
    if app and not path:
        raise ValueError("menu_path: empty path")
    if not 1 <= len(rest) <= 2:
        raise ValueError(f"menu_path: expected [navbar] or [navbar, item] after the app, got {rest!r}")
    if app:
        await sidebar(page, cursor, path[0])
    await navbar(page, cursor, rest[0])
    if len(rest) == 2:
        await dropdown_item(page, cursor, rest[1])


async def refresh_list(page, cursor: CursorState, settle: float = 1.5) -> None:
    """Reload an Odoo list KEEPING its filter, the way a user does: click the (empty) search box
    and press Enter. page.reload() flashes the unfiltered list — real data on screen."""
    await click_box(page, page.locator(".o_searchview_input"), cursor)
    await page.keyboard.press("Enter")
    await asyncio.sleep(settle)


async def press_button(page, cursor: CursorState, label: str, scope: str = ".o_form_view",
                       wait_toast: bool = True, pause: float = MENU_PAUSE,
                       settle: float = TOAST_SETTLE, toast_timeout: float = 90.0,
                       prev_timeout: float = 15.0) -> None:
    """Click a visible `<button>` inside `scope` (whole label, case-insensitive: the theme may
    upper-case the DOM text). wait_toast=True (a button that raises a notification): wait for
    the previous `.o_notification` to go away, click, wait for the new one, then hold `settle`
    seconds so the toast is on screen. wait_toast=False (a button that navigates): click only —
    the caller then waits for the opened view with wait_ready. A toast that never comes raises
    (a timeout from Playwright); an error dialog shown instead of the toast raises RuntimeError
    at once with the dialog text; a previous toast that never goes away is only reported."""
    button = page.locator(f"{scope} button:visible").filter(has_text=exact_text_re(label)).first
    if wait_toast:
        try:
            await page.wait_for_function("() => !document.querySelector('.o_notification')",
                                         timeout=prev_timeout * 1000)
        except Exception as exc:  # a stuck old toast must not stop the take: report and go on
            print(f"[click_helpers] press_button({label!r}): previous toast still shown "
                  f"after {prev_timeout}s ({type(exc).__name__}), clicking anyway", file=sys.stderr)
    await asyncio.sleep(pause)
    await click_box(page, button, cursor)
    if wait_toast:
        await page.wait_for_selector(f".o_notification, {ERROR_DIALOG}", state="visible",
                                     timeout=toast_timeout * 1000)
        dialog = page.locator(ERROR_DIALOG)
        if await dialog.count():  # Validation Error / Odoo Server Error: no toast will ever come
            text = (await dialog.first.inner_text()).strip()[:300]
            raise RuntimeError(f"press_button({label!r}): an error dialog appeared instead of a toast: {text!r}")
        await asyncio.sleep(settle)
