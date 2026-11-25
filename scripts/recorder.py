"""recorder.py — orchestration template cho record-demo-video (Xvfb + ffmpeg + Playwright).

Đây là TEMPLATE để import vào demo script riêng của bạn, KHÔNG phải script chạy thẳng —
mỗi project cần một demo script khác nhau (site khác, bước UI khác). Phần Xvfb/ffmpeg/
launch-context là generic, verify rồi, không cần viết lại. Xem REFERENCE.md §1-2.

Ví dụ dùng (asyncio):

    from recorder import DemoRecorder
    from click_helpers import CursorState, click_box, click_text, wait_ready

    async def main():
        rec = DemoRecorder(display=":99", out_path="raw_capture.mp4",
                            profile_dir="/tmp/demo-profile-myfeature")
        rec.patch_chrome_prefs(lang="vi")
        async with rec.session() as (page, cursor):
            await page.goto("https://your-odoo-instance/odoo", wait_until="domcontentloaded")
            await wait_ready(page, ".o_login_form, .o_action_manager")
            rec.mark("open_login")   # giây tính từ lúc ffmpeg bắt đầu ghi, không phải từ đây
            ...
    asyncio.run(main())
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from build_srt import LAST_CUE_DURATION, READ_CPS, cue_seconds
from click_helpers import CursorState
from patch_chrome_prefs import patch_chrome_prefs

# Platforms with no Xvfb/x11grab: they record through Playwright (capture="video"). Read at call time so
# a test can pin sys.platform.
VIDEO_PLATFORMS = ("darwin", "win32")


class DemoRecorder:
    def __init__(self, display: str, out_path: str, profile_dir: str,
                 resolution: str = "1920x1080", chrome_binary: str = "/usr/bin/google-chrome",
                 xvfb_startup_wait: float = 1.0, kiosk: bool = True, capture: str = "auto"):
        # capture: "x11" = Xvfb + ffmpeg x11grab (Linux). "video" = Playwright record_video_dir, không cần
        # Xvfb/ffmpeg-grab/quyền Screen Recording (macOS, Windows). "auto" = video trên darwin/win32, x11 nơi khác.
        if capture not in ("auto", "x11", "video"):
            raise ValueError("capture phải là auto | x11 | video, nhận: %r" % (capture,))
        self.capture = ("video" if sys.platform in VIDEO_PLATFORMS else "x11") if capture == "auto" else capture
        self.display = display
        # kiosk=True: Chrome --kiosk — không thanh tab/địa chỉ trong video (trước đây lọt vào
        # MỌI video). Xvfb không có window manager nên cửa sổ kiosk
        # vẫn cần --window-size (thiếu thì chỉ ~2/3 màn hình). False = cửa sổ thường như cũ.
        self.kiosk = kiosk
        self.out_path = out_path
        self.profile_dir = profile_dir
        self.resolution = resolution
        self.chrome_binary = chrome_binary
        self.xvfb_startup_wait = xvfb_startup_wait
        self._xvfb_proc: subprocess.Popen | None = None
        self._ffmpeg_proc: subprocess.Popen | None = None
        # Mốc 0 của video (time.monotonic() ngay sau khi spawn ffmpeg). MARK phải đo từ
        # đây: đo từ lúc demo script bắt đầu thì lệch cả khoảng Xvfb + Chrome + tải trang
        # (đo thật 10.49 s trên Odoo 17) → phụ đề trễ và không biết cắt lead-in bao nhiêu.
        self.video_t0: float | None = None
        # Log riêng của ffmpeg: in chung stdout với MARK thì dòng tiến độ `\r` của ffmpeg chen vào giữa
        # các dòng MARK và `grep ^MARK` bỏ sót.
        self.ffmpeg_log = f"{out_path}.ffmpeg.log"
        self._ffmpeg_log_fh = None
        self._pace: tuple[dict, float, float] | None = None   # (narration, min_cue, cps) — pace_from()
        self._last_mark: tuple[float, str] | None = None      # (giây video, step) của mark gần nhất

    def patch_chrome_prefs(self, lang: str = "vi") -> None:
        """Chạy TRƯỚC launch — patch profile sạch, xem REFERENCE.md §5."""
        patch_chrome_prefs(self.profile_dir, lang=lang)

    def start_xvfb(self) -> None:
        self._xvfb_proc = subprocess.Popen(
            ["Xvfb", self.display, "-screen", "0", f"{self.resolution}x24", "-nolisten", "tcp"]
        )
        time.sleep(self.xvfb_startup_wait)  # Xvfb cần vài trăm ms để socket sẵn sàng
        # Display đã có người dùng (session khác đang quay) → Xvfb của ta thoát ngay, còn ffmpeg + trình duyệt
        # sẽ quay/mở trên màn hình của SESSION KHÁC mà không ai báo (236).
        if self._xvfb_proc.poll() is not None:
            raise RuntimeError(f"Xvfb {self.display} thoát ngay (rc={self._xvfb_proc.returncode}) — display "
                               "đang có người dùng? Chọn số display khác")

    def start_ffmpeg(self) -> None:
        env = {**os.environ, "DISPLAY": self.display}
        # Trạng thái theo TỪNG take: một recorder chạy session() lần 2 không được mang mark cuối của
        # take 1 (đồng hồ take 1 → mark_paced đầu take 2 ngủ cả độ dài take 1) hay log theo out_path cũ.
        self._last_mark = None
        self.ffmpeg_log = f"{self.out_path}.ffmpeg.log"
        self._ffmpeg_log_fh = open(self.ffmpeg_log, "wb")
        try:
            self._ffmpeg_proc = subprocess.Popen(
                [
                    "ffmpeg", "-f", "x11grab", "-video_size", self.resolution,
                    # -draw_mouse 0: mouse.move() của Playwright đi qua CDP, KHÔNG di con trỏ X
                    # thật — con trỏ OS đứng yên giữa màn hình suốt video (đo 2026-10-19), nên
                    # vẽ nó chỉ thêm 1 mũi tên lạc cạnh mũi tên overlay, không phải fallback.
                    # PHẢI đứng TRƯỚC -i: đó là option của input x11grab — đặt sau -i ffmpeg coi
                    # là option output và bỏ qua, con trỏ X (bàn tay khi Chrome hover phần tử
                    # click được) vẫn bị ghi giữa video (đo lại cùng cảnh:
                    # sau -i 131 px con trỏ, trước -i 0 px).
                    "-draw_mouse", "0", "-i", f"{self.display}.0",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-pix_fmt", "yuv420p", "-y", self.out_path,
                ],
                env=env, stdin=subprocess.PIPE, stdout=self._ffmpeg_log_fh, stderr=self._ffmpeg_log_fh,
            )
        except BaseException:
            # Popen raise (không có ffmpeg trên PATH…): stop_ffmpeg() không đóng được vì _ffmpeg_proc là None.
            self._ffmpeg_log_fh.close()
            self._ffmpeg_log_fh = None
            raise
        self.video_t0 = time.monotonic()

    def video_elapsed(self) -> float:
        """Số giây kể từ lúc ffmpeg bắt đầu ghi — trục thời gian của raw capture."""
        if self.video_t0 is None:
            raise RuntimeError("video_elapsed()/mark() chỉ gọi được sau start_ffmpeg() (bên trong session())")
        return time.monotonic() - self.video_t0

    def mark(self, step: str) -> None:
        """In `MARK <giây từ đầu video> <step>` cho build_srt.py (REFERENCE.md §7).
        step rỗng/có khoảng trắng → ValueError NGAY lúc quay: regex MARK của build_srt chỉ
        nhận 1 token, dòng `MARK 2.70 open login` bị bỏ qua im lặng → mất phụ đề bước đó."""
        if not step or any(ch.isspace() for ch in step):
            raise ValueError(f"mark(): step phải là 1 token không khoảng trắng (vd open_login), nhận {step!r}")
        elapsed = self.video_elapsed()
        self._last_mark = (elapsed, step)
        print(f"MARK {elapsed:.2f} {step}", flush=True)

    def pace_from(self, narration: dict, min_cue: float = LAST_CUE_DURATION, cps: float = READ_CPS) -> None:
        """Bật nhịp phụ đề cho mark_paced(): narration = cùng dict `{step: {lang: text}}` truyền cho
        build_srt.py. Mark bắn nhanh theo UI làm cue chỉ hiện 2-4 s, không đọc kịp."""
        self._pace = (narration, min_cue, cps)

    def cue_left(self) -> float:
        """Số giây bước vừa mark còn phải nằm trên màn hình để đọc kịp lời của nó — theo câu DÀI
        nhất trong mọi ngôn ngữ (một take dùng cho mọi bản phụ đề). 0 khi chưa pace_from()/chưa mark
        hoặc bước không có lời."""
        if self._pace is None or self._last_mark is None:
            return 0.0
        narration, min_cue, cps = self._pace
        texts = [t for t in narration.get(self._last_mark[1], {}).values() if t]
        if not texts:
            return 0.0
        need = max(cue_seconds(t, min_cue, cps) for t in texts)
        return max(0.0, self._last_mark[0] + need - self.video_elapsed())

    async def mark_paced(self, step: str) -> None:
        """Như mark() nhưng chờ cue của bước TRƯỚC đủ lâu để đọc (cue_left) rồi mới mark.
        Chưa pace_from() → RuntimeError: lặng lẽ chạy như mark() sẽ quay cả take với cue 2-4 s mà chỉ
        thấy sau khi burn phụ đề."""
        if self._pace is None:
            raise RuntimeError("mark_paced(): gọi rec.pace_from(narration) trước (cùng dict narration của build_srt.py)")
        await asyncio.sleep(self.cue_left())
        self.mark(step)

    def stop_ffmpeg(self) -> None:
        """Dừng graceful ('q' qua stdin) để ffmpeg mux trailer đúng — SIGKILL làm hỏng file."""
        if self._ffmpeg_proc is None:
            return
        try:
            self._ffmpeg_proc.communicate(input=b"q", timeout=10)
        except subprocess.TimeoutExpired:
            self._ffmpeg_proc.send_signal(signal.SIGINT)
            self._ffmpeg_proc.wait(timeout=10)
        finally:
            if self._ffmpeg_log_fh is not None:
                self._ffmpeg_log_fh.close()
                self._ffmpeg_log_fh = None

    def stop_xvfb(self) -> None:
        if self._xvfb_proc is not None:
            self._xvfb_proc.terminate()
            self._xvfb_proc.wait(timeout=5)

    def _ffmpeg_path(self) -> str:
        path = shutil.which("ffmpeg")
        if path is None:
            raise RuntimeError("không có ffmpeg trên PATH (macOS: brew install ffmpeg; Windows: winget install "
                               "Gyan.FFmpeg, rồi mở lại terminal) — cần để đổi webm của "
                               "Playwright sang mp4; kiểm tra TRƯỚC khi quay để một take dài không mất ở bước cuối")
        return path

    def _convert_video(self, webm_path: str) -> None:
        """webm của Playwright (VP8, 25 fps) → mp4 H.264 yuv420p cùng thông số với đường x11grab, để
        trim_black_tail/build_srt/burn_subtitles dùng nguyên. Lỗi ffmpeg → RuntimeError chỉ log."""
        with open(self.ffmpeg_log, "wb") as log:
            rc = subprocess.run(
                [self._ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", webm_path,
                 "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-y", self.out_path],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            ).returncode
        if rc != 0:
            raise RuntimeError(f"ffmpeg đổi webm → mp4 lỗi rc={rc} — xem {self.ffmpeg_log}")

    @contextlib.asynccontextmanager
    async def _session_video(self):
        """macOS (capture="video"): Chromium headless + Playwright record_video_dir. Không Xvfb, không x11grab,
        không quyền Screen Recording (TCC), không chạm màn hình thật của người dùng. Trang được vẽ trong tiến trình
        trình duyệt nên mũi tên overlay (DOM) vẫn nằm trong video; con trỏ OS thì không bao giờ có. Mốc 0 =
        ngay sau khi launch trả về; video ĐI TRƯỚC mark 0.02-0.13 s, không trôi trong take 100 s (đo 2026-11-18)
        — phụ đề hiện trễ nhẹ, vô hại. Playwright chỉ chốt file webm khi ctx.close(), và
        chỉ quay trang `session()` yield (popup/tab mới có webm riêng, bị bỏ). Đuôi video là khung hình ĐÓNG BĂNG
        0.4-5 s (không đen → trim_black_tail không cắt được). Dùng viewport= (headless không có cửa sổ OS) —
        KHÁC đường x11 vốn dùng no_viewport + --window-size, đừng "sửa" lại."""
        try:
            from playwright.async_api import async_playwright  # import trễ — optional dep
        except ImportError as exc:
            raise RuntimeError("playwright không có trong interpreter này — trên macOS/Windows chạy demo script bằng "
                               "python có Playwright (Windows: py -3 -m pip install playwright), không phải "
                               "`uvx --from invisible-playwright-mcp python`") from exc

        self._ffmpeg_path()  # fail sớm: thiếu ffmpeg thì không quay
        self._last_mark = None
        self.ffmpeg_log = f"{self.out_path}.ffmpeg.log"
        # Mở log TRƯỚC khi quay như start_ffmpeg() (đường x11): out_path trỏ vào thư mục không tồn tại phải nổ ngay,
        # không phải sau cả take (take chạy hết rồi FileNotFoundError, webm bị xoá).
        open(self.ffmpeg_log, "wb").close()
        width, height = (int(v) for v in self.resolution.split("x"))
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        video_dir = tempfile.mkdtemp(prefix="rdv-video-")
        webm_path = None
        body_failed = False   # cờ tường minh: sys.exc_info() cũng true khi caller đang ở trong một handler except
        try:
            async with async_playwright() as p:
                ctx = await p.chromium.launch_persistent_context(
                    user_data_dir=self.profile_dir,
                    # chrome_binary mặc định là đường dẫn Linux; không có file → Chromium đi kèm Playwright.
                    executable_path=self.chrome_binary if Path(self.chrome_binary).exists() else None,
                    headless=True,
                    viewport={"width": width, "height": height},
                    record_video_dir=video_dir,
                    record_video_size={"width": width, "height": height},
                    args=["--disable-notifications", "--disable-infobars"],
                )
                self.video_t0 = time.monotonic()
                page = None
                try:
                    await ctx.add_init_script(path=str(Path(__file__).with_name("cursor_overlay.js")))
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    cursor = CursorState(width / 2, height / 2)
                    await page.mouse.move(cursor.x, cursor.y)   # xem session() — chuột thật về chỗ CursorState
                    yield page, cursor
                    await asyncio.sleep(self.cue_left())
                finally:
                    if len(ctx.pages) > 1:
                        print("record-demo-video: có popup/tab mới — macOS/Windows chỉ quay trang chính, tab khác KHÔNG "
                              "vào video (ở lại một tab)", file=sys.stderr)
                    await ctx.close()
                    if page is not None and page.video is not None:
                        webm_path = await page.video.path()
        except BaseException:
            body_failed = True
            raise
        finally:
            # webm là bản ghi DUY NHẤT: chỉ xoá khi đổi mp4 đã TRẢ VỀ. Allow-list, không phải cờ đặt trong
            # `except Exception` — KeyboardInterrupt/SystemExit lọt qua nó và làm mất webm (đo: 1 SIGINT
            # trên Python 3.9, 2 trên 3.11).
            keep_video_dir = webm_path is not None
            try:
                if webm_path is not None:
                    self._convert_video(str(webm_path))
                    keep_video_dir = False
            except Exception as exc:
                # đổi mp4 lỗi thì giữ lại và nêu đường dẫn để đổi tay, không xoá.
                msg = f"{exc} — webm gốc giữ tại {webm_path}"
                if not body_failed:
                    raise RuntimeError(msg) from exc
                print(f"record-demo-video: {msg} (lỗi gốc của demo script được giữ)", file=sys.stderr)
            finally:
                if not keep_video_dir:
                    shutil.rmtree(video_dir, ignore_errors=True)

    @contextlib.asynccontextmanager
    async def session(self):
        """Context manager: start Xvfb + ffmpeg, launch Chrome, yield (page, cursor),
        dọn dẹp theo thứ tự ngược khi thoát (Chrome → ffmpeg → Xvfb). capture="video" (macOS) → xem
        _session_video()."""
        if self.capture == "video":
            async with self._session_video() as pc:
                yield pc
            return

        from playwright.async_api import async_playwright  # import trễ — optional dep

        # start_xvfb/start_ffmpeg bọc try/finally NGAY TỪ ĐÂY: nếu launch_persistent_context
        # raise (path Chrome sai, profile lock tranh chấp — xem REFERENCE.md §1, hoặc Chrome
        # crash lúc khởi động), thiếu try/finally ở đây làm Xvfb + ffmpeg LEAK chạy nền vô
        # thời hạn (ffmpeg tiếp tục ghi màn hình đen, phình file không giới hạn) — bug 🟠 Major
        # đã gặp thật khi review.
        self.start_xvfb()
        try:
            self.start_ffmpeg()
            try:
                Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
                width, height = (int(v) for v in self.resolution.split("x"))
                async with async_playwright() as p:
                    ctx = await p.chromium.launch_persistent_context(
                        user_data_dir=self.profile_dir,
                        executable_path=self.chrome_binary,
                        headless=False,
                        # no_viewport + cửa sổ đúng bằng màn Xvfb tại (0,0): với viewport=
                        # 1920x1080, Playwright mở cửa sổ 1928x1211 ở (10,10) → ~97 px đáy
                        # trang nằm NGOÀI khung x11grab (đo 2026-10-19).
                        # Vùng trang thật = innerHeight (~992 px; kiosk: gần trọn màn hình, cửa sổ
                        # Xvfb hụt 1 px — đo 1919x1079) — click_helpers đọc nó.
                        no_viewport=True,
                        args=["--disable-notifications", "--disable-infobars",
                              # +1 px: trên Xvfb cửa sổ Chrome ra hụt 1 px (W-1 x H-1) → mọi frame
                              # có viền đen 1 px phải + đáy; W+1 x H+1 phủ kín (đo 2026-10-21: 720/1280
                              # px đen → 0, cả kiosk lẫn không).
                              "--window-position=0,0", f"--window-size={width + 1},{height + 1}"]
                             + (["--kiosk"] if self.kiosk else []),
                        # Playwright tự thêm --no-sandbox (bản cũ thêm cả --enable-automation,
                        # 1.63 thì không — bỏ qua vẫn vô hại); --no-sandbox hiện thanh cảnh báo
                        # vàng trong video mà --disable-infobars KHÔNG ẩn được (đo 2026-10-19).
                        ignore_default_args=["--no-sandbox", "--enable-automation"],
                        env={**os.environ, "DISPLAY": self.display},
                    )
                    try:
                        overlay_js = str(Path(__file__).with_name("cursor_overlay.js"))
                        await ctx.add_init_script(path=overlay_js)
                        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                        # Bắt đầu ở GIỮA màn hình, không (0,0): lần tải đầu Chrome báo con trỏ X
                        # thật (giữa màn hình) như một mousemove → mũi tên hiện ở giữa; từ (0,0)
                        # thì smooth_move đầu tiên nhảy ~1040 px trong 1 frame (teleport —
                        # đo khi quay thật qua session()).
                        cursor = CursorState(width / 2, height / 2)
                        # Đưa chuột THẬT của trình duyệt về đúng chỗ CursorState nói: nếu không, nó nằm ở (0,0)
                        # (trên navbar) → nấc wheel đầu tiên không cuộn gì → bring_into_view nhảy scrollIntoView
                        # 3380 px trong 1 nhịp.
                        await page.mouse.move(cursor.x, cursor.y)
                        yield page, cursor
                        # Thoát bình thường: giữ khung hình đủ lâu cho cue cuối (0 khi không pace_from).
                        await asyncio.sleep(self.cue_left())
                    finally:
                        await ctx.close()
            finally:
                self.stop_ffmpeg()
        finally:
            self.stop_xvfb()
