"""stealth_recorder.py — driver MẶC ĐỊNH của record-demo-video: invisible_playwright (Firefox stealth, engine
của plugin `invisible-playwright-mcp`). Trình duyệt TỰ vẽ con trỏ mũi tên + vòng gợn khi click và tự di chuột
theo đường cong kiểu người → không cần overlay con trỏ; kiosk → không thanh tab/địa chỉ. Xvfb + ffmpeg + MARK
dùng lại nguyên DemoRecorder (recorder.py). Xem REFERENCE.md §0.

Chạy demo script bằng interpreter của plugin (vendored Playwright — `import playwright` KHÔNG có ở đó):

    uvx --from invisible-playwright-mcp python your_demo_script.py

Ví dụ (asyncio) — y hệt DemoRecorder, chỉ đổi cách tạo recorder:

    from stealth_recorder import make_recorder
    from click_helpers import click_box, point_at, wait_ready

    async def main():
        rec = make_recorder(display=":99", out_path="raw_capture.mp4", profile_dir="/tmp/demo-profile-x")
        async with rec.session() as (page, cursor):
            ...
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import os
import sys

from click_helpers import CursorState
from recorder import VIDEO_PLATFORMS, DemoRecorder

LAUNCH_HINT = "uvx --from invisible-playwright-mcp python <your_demo_script.py>"


def stealth_available() -> bool:
    """invisible_playwright import được trong interpreter đang chạy?"""
    return importlib.util.find_spec("invisible_playwright") is not None


class StealthRecorder(DemoRecorder):
    """DemoRecorder chạy Firefox stealth thay Chrome. profile_dir/chrome_binary không dùng (Firefox có profile
    tạm riêng); "Save password?" tắt bằng pref `signon.rememberSignons` thay cho patch_chrome_prefs."""

    def __init__(self, display: str, out_path: str, resolution: str = "1920x1080", seed: int = 7,
                 locale: str = "en-US", xvfb_startup_wait: float = 1.0, kiosk: bool = True):
        # capture="x11": session() của lớp này chỉ có đường Xvfb — không để .capture trên darwin tự báo "video".
        super().__init__(display, out_path, profile_dir="", resolution=resolution,
                         xvfb_startup_wait=xvfb_startup_wait, kiosk=kiosk, capture="x11")
        self.seed = seed
        self.locale = locale

    def patch_chrome_prefs(self, lang: str = "vi") -> None:
        """Không áp dụng cho Firefox — pref tắt lưu mật khẩu đã truyền lúc launch."""

    @contextlib.asynccontextmanager
    async def session(self):
        """Start Xvfb + ffmpeg, launch Firefox stealth, yield (page, cursor); dọn ngược thứ tự khi thoát.
        cursor.native_motion=True: click_helpers đi MỘT mouse.move/đích, thư viện tự vẽ đường đi."""
        from invisible_playwright.async_api import InvisiblePlaywright  # import trễ — optional dep

        # InvisiblePlaywright không nhận env riêng → DISPLAY của tiến trình; try/finally NGOÀI cùng để trả lại
        # cả khi start_xvfb() raise (display bận).
        old_display = os.environ.get("DISPLAY")
        os.environ["DISPLAY"] = self.display
        try:
            self.start_xvfb()
            try:
                self.start_ffmpeg()
                try:
                    width, height = (int(v) for v in self.resolution.split("x"))
                    # --width/--height: probe một trang Odoo (kiosk trần) thấy cửa sổ ~2/3 màn hình; với new_context(viewport,
                    # screen) như dưới, khung hình giống hệt khi bỏ chúng (đo 2026-10-21) — giữ cho chắc, vô hại.
                    args = ["--width=%d" % width, "--height=%d" % height] + (["--kiosk"] if self.kiosk else [])
                    # pin screen.*: từ invisible-playwright 0.45.0 / engine 20261104, cửa sổ --kiosk lấy kích thước
                    # màn hình của persona fingerprint (seed 7 → 1920x1080, seed 5 → 2560x1440), bỏ qua viewport và
                    # màn Xvfb ⇒ phần tràn ra ngoài Xvfb không vào video. Ghim màn persona = độ phân giải Xvfb
                    # (đo 2026-11-06: 0.44.1 và 0.45.0, seed 5 và 7 → inner = W x H).
                    # screen.dpr 1.0 nữa: width/height là px THIẾT BỊ, trang đọc px CSS = px thiết bị / DPR và engine
                    # render theo DPR của persona ⇒ seed DPR 1.25 (6, 20) cho inner 1024x576, trang phóng to mà
                    # page.viewport_size vẫn báo 1280x720 (đo 2026-11-06; 57/300 seed đổi DPR
                    # giữa invisible-core 51.48.0 và 55.50.0).
                    async with InvisiblePlaywright(seed=self.seed, headless=False, show_cursor=True,
                                                   locale=self.locale, extra_args=args,
                                                   pin={"screen.width": width, "screen.height": height,
                                                        "screen.dpr": 1.0},
                                                   extra_prefs={"signon.rememberSignons": False}) as browser:
                        ctx = await browser.new_context(viewport={"width": width, "height": height},
                                                        screen={"width": width, "height": height})
                        try:
                            page = await ctx.new_page()
                            cursor = CursorState(width / 2, height / 2, native_motion=True)
                            # Chuột thật của trình duyệt về đúng chỗ CursorState nói (xem recorder.py session()).
                            await page.mouse.move(cursor.x, cursor.y)
                            yield page, cursor
                            await asyncio.sleep(self.cue_left())   # cue cuối — xem recorder.py session()
                        finally:
                            await ctx.close()
                finally:
                    self.stop_ffmpeg()
            finally:
                self.stop_xvfb()
        finally:
            if old_display is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = old_display


def _accepted(cls, kw: dict) -> dict:
    """Giữ các kw mà constructor của cls nhận — một demo script truyền kw của cả 2 driver (seed/locale cho
    stealth, chrome_binary cho Chrome) mà không vỡ ở driver kia (TypeError).
    Chỉ bỏ tên mà driver KIA nhận; tên không driver nào nhận (gõ nhầm `kisok=`, `resoluton=`) → TypeError,
    không lặng lẽ quay với giá trị mặc định."""
    known = set()
    for c in (StealthRecorder, DemoRecorder):
        known |= set(inspect.signature(c.__init__).parameters)
    unknown = sorted(set(kw) - known)
    if unknown:
        raise TypeError("make_recorder: tham số không driver nào nhận: %s" % ", ".join(unknown))
    params = inspect.signature(cls.__init__).parameters
    return {k: v for k, v in kw.items() if k in params}


def make_recorder(display: str, out_path: str, profile_dir: str, driver: str = "auto", **kw) -> DemoRecorder:
    """driver="auto": stealth khi invisible_playwright import được (chạy qua LAUNCH_HINT), ngược lại Chrome +
    mũi tên overlay (DemoRecorder) kèm 1 dòng stderr. macOS (darwin): luôn Chrome, không dòng stderr nào. "stealth" / "chrome" để ép. kw của driver kia bị bỏ qua."""
    if driver not in ("auto", "stealth", "chrome"):
        raise ValueError("driver phải là auto | stealth | chrome, nhận: %r" % (driver,))
    if sys.platform in VIDEO_PLATFORMS:
        # StealthRecorder.session() = Xvfb + x11grab: chưa có đường macOS/Windows (Firefox stealth + record video chưa thử).
        if driver == "stealth":
            raise RuntimeError("driver stealth chưa hỗ trợ macOS/Windows (cần Xvfb + x11grab) — dùng driver=\"chrome\" "
                               "(DemoRecorder, capture=\"video\") hoặc quay trên máy Linux")
        driver = "chrome"
    if driver == "stealth" or (driver == "auto" and stealth_available()):
        if not stealth_available():
            raise RuntimeError("invisible_playwright không có trong interpreter này — chạy: " + LAUNCH_HINT)
        return StealthRecorder(display, out_path, **_accepted(StealthRecorder, kw))
    if driver == "auto":
        print("record-demo-video: invisible_playwright không có → dùng Chrome + mũi tên overlay. Driver stealth: "
              + LAUNCH_HINT, file=sys.stderr)
    return DemoRecorder(display, out_path, profile_dir, **_accepted(DemoRecorder, kw))
