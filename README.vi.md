# record-video-skills — hướng dẫn cho người quay video (Windows)

Plugin cho [Claude Code](https://claude.com/claude-code): nhờ Claude **quay video hướng dẫn sử dụng** một chức năng trên web/Odoo. Video có con trỏ mũi tên nhìn thấy được (có gợn sóng khi click), mỗi lần click có một câu thuyết minh, phụ đề được ghi thẳng vào video, **mỗi ngôn ngữ một video** (tiếng Việt, tiếng Hàn, tiếng Anh).

> **Trạng thái:** phần quay trên Windows đã được viết và có test tự động, nhưng **chưa được kiểm chứng trên máy Windows 11 thật**. Hãy coi video đầu tiên là bản chạy thử và báo lại mọi điều bất thường.

## 1. Chuẩn bị máy (khoảng 15 phút, làm một lần)

Mở **PowerShell** (không cần quyền quản trị) và chạy lần lượt:

```powershell
winget install Python.Python.3.12 --accept-source-agreements --accept-package-agreements
winget install Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
```

**Đóng PowerShell, mở cửa sổ MỚI** (chương trình vừa cài chỉ xuất hiện ở cửa sổ mới), rồi:

```powershell
py -3 -m pip install playwright
py -3 -m playwright install chromium
```

Lệnh cuối tải trình duyệt Chromium (khoảng 150 MB); nếu công ty có proxy hoặc phần mềm diệt virus chặn, nhờ bộ phận IT.

## 2. Cài plugin

Trong Claude Code gõ:

```
/plugin marketplace add namchamvinhcuu/record-video-skills
/plugin install record-video-skills@record-video
```

Sau đó kiểm tra máy:

```
/record-video-skills:check-env
```

Mỗi dòng báo `OK`, `MISSING` hoặc `SKIP`. Dòng `MISSING` đi kèm đúng câu lệnh cài đặt; Claude sẽ hỏi bạn trước khi chạy lệnh cài. Cài xong mở cửa sổ mới và chạy lại cho đến khi báo `READY`.

## 3. Đặt tài khoản demo (KHÔNG gõ mật khẩu vào khung chat)

Chỉ dùng **hệ thống demo** và **một tài khoản demo dùng tạm**, không bao giờ dùng hệ thống thật hay mật khẩu bạn dùng ở nơi khác. Trong CÙNG cửa sổ PowerShell sẽ chạy Claude Code:

```powershell
$env:ODOO_DEMO_URL = "https://your-odoo-instance"
$env:ODOO_DEMO_LOGIN = Read-Host "Tên đăng nhập demo"
$secure = Read-Host "Mật khẩu demo (không hiện trên màn hình)" -AsSecureString
$env:ODOO_DEMO_PASSWORD = [System.Net.NetworkCredential]::new("", $secure).Password
claude
```

- Thay `https://your-odoo-instance` bằng địa chỉ hệ thống demo (bắt buộc `https`, không chứa tên đăng nhập trong địa chỉ).
- Phải chạy `claude` **từ chính cửa sổ này**: Claude đang chạy không nhìn thấy biến đặt sau đó.
- Không dùng `setx` (nó lưu mật khẩu vĩnh viễn vào máy). Đóng cửa sổ là biến mất.

## 4. Quay video đầu tiên

```
/record-video-skills:record-demo quay hướng dẫn chức năng <tên chức năng>, ngôn ngữ vi và ko, hệ thống demo đã đặt ở trên
```

Claude sẽ hỏi thêm những gì còn thiếu (luồng thao tác, ngôn ngữ), viết kịch bản đi từng menu bằng con trỏ, quay và tạo mỗi ngôn ngữ một file `final_<ngôn ngữ>.mp4`.

## 5. Claude sẽ xin phép chạy những gì

Claude Code hỏi trước mỗi lệnh. Bạn sẽ thấy các lệnh sau, đều bình thường:

- `py -3 ...` chạy các script của plugin (kiểm tra máy, dựng phụ đề, ghi phụ đề vào video, quay).
- `ffmpeg` / `ffprobe` để chuyển và ghi phụ đề vào video.
- `winget install ...`, `py -3 -m pip install ...`, `py -3 -m playwright install chromium` — **chỉ khi bạn đồng ý** cài thứ còn thiếu.

**Đừng chọn "luôn cho phép" (always allow) cho `py -3 *` hoặc `python *`**: lệnh được cho phép vĩnh viễn có thể đọc biến mật khẩu demo mà không hỏi lại. Chỉ cho phép từng lệnh một.

Từ chối mọi lệnh lạ: gửi mật khẩu đi đâu đó, chạy chương trình không liên quan, xoá thư mục ngoài thư mục làm việc.

## 6. Riêng tư và dọn dẹp

- Video hiện **mọi dữ liệu đang có trên màn hình**: chỉ quay hệ thống demo, không bao giờ quay hệ thống có dữ liệu thật của khách.
- Ô mật khẩu được che trong video; mật khẩu không bị in ra, không ghi vào file. **Tên đăng nhập thì hiện rõ trong cảnh đăng nhập** (phần đầu video, được cắt khỏi video cuối bằng `--cut`): dùng tên tài khoản demo, không dùng email cá nhân.
- File quay thô `raw_capture.mp4` **vẫn chứa cảnh đăng nhập**: xoá nó (và mọi thư mục `rdv-video-*` trong thư mục tạm) sau khi đã có video cuối.
- Trình duyệt dùng một hồ sơ tạm riêng và tự xoá khi xong; không dùng hồ sơ trình duyệt thật của bạn.
- Đặt thư mục làm việc ngoài thư mục được đồng bộ OneDrive nếu có thể (file quay thô rất nặng).

## 7. Lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| `python` mở Microsoft Store | Đó là lối tắt giả. Dùng `py -3`, hoặc cài lại bằng `winget install Python.Python.3.12` rồi mở cửa sổ mới. |
| Vừa cài xong vẫn báo thiếu | Đóng và mở cửa sổ PowerShell mới (và chạy lại `claude` từ đó). |
| Phụ đề tiếng Hàn là ô vuông | `check-env` báo thiếu phông; cài gói ngôn ngữ Hàn của Windows hoặc phông Noto Sans CJK KR. |
| `burn_subtitles` báo ffmpeg thiếu libass | Cài bản ffmpeg đầy đủ: `winget install Gyan.FFmpeg`. |
| Video đứng hình ở cuối | Bình thường: phần đuôi là khung hình cuối đứng yên; Claude chọn điểm cắt cuối. |
| Video đứng hình giữa chừng | Thao tác mở thêm tab/cửa sổ mới: chỉ trang chính được quay. Ở lại một tab. |

## 8. Gỡ cài đặt

```
/plugin uninstall record-video-skills@record-video
/plugin marketplace remove record-video
```

Giấy phép: MIT (xem [LICENSE](LICENSE)). Tài liệu tiếng Anh: [README.md](README.md).
