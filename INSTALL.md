# Hướng dẫn cài đặt tự động và đẩy mã lên GitHub

> **Mục tiêu**: Cho phép agent Antigravity tự động cài đặt toàn bộ hệ thống Closed‑Loop Task Engine trên môi trường Windows và đẩy toàn bộ code lên repository GitHub đã cấu hình.

## 1. Chuẩn bị môi trường
1. **Git**: Đảm bảo `git` đã được cài đặt và cấu hình credential (HTTPS token hoặc SSH key) để có thể push.
2. **PowerShell**: Mặc định trên Windows.
3. **Docker Desktop** (tuỳ chọn): Nếu muốn dùng stack Loki/Grafana, bật Docker và chắc chắn daemon đang chạy.
4. **Python 3.12+**: Đã có trong môi trường Antigravity.

## 2. Tự động cài đặt toàn bộ hệ thống
Chạy lệnh sau trong PowerShell (đường dẫn tới thư mục dự án):
```powershell
cd D:\04_Tools\Workflow\Close_workflow
# Đánh dấu các script để chạy được (PowerShell sẽ tự chuyển đổi line endings)
git -C . add -A
# Cài đặt môi trường ảo (không bắt buộc nếu đã có Python)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# Cài đặt các phụ thuộc nếu có (ở đây chỉ cần Antigravity)
# Khởi động Docker stack (nếu Docker đang chạy)
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker-compose -f docker-compose.yml up -d
    docker-compose -f docker-compose-logging.yml up -d
}
# Khởi động listener (daemon) và sub‑agent
python listener.py &
# Hoặc dùng script PowerShell có sẵn
./install.ps1
```
Lệnh trên sẽ:
- Khởi tạo môi trường Python ảo.
- Khởi chạy Docker containers (Redis, Loki, Grafana) nếu Docker đang chạy.
- Khởi động **listener** để lắng nghe `incoming_tasks/`.
- Định nghĩa sub‑agent `closed_loop_task_engine` (được tự động tạo trong `close_workflow_cli.py`).

## 3. Đẩy toàn bộ dự án lên GitHub (force)
Sau khi cài đặt và kiểm tra mọi thứ hoạt động, chạy:
```powershell
git add -A
git commit -m "Automated install & initial push"
git push -u origin main --force
```
Nếu remote repo có nhánh `main` khác, lệnh `--force` sẽ ghi đè toàn bộ lịch sử trên GitHub bằng trạng thái hiện tại của thư mục.

## 4. Kiểm tra
- Mở https://github.com/Heizdoobert/Close_workflow và xác nhận các file đã được cập nhật.
- Truy cập `http://localhost:3000` để xem giao diện Grafana (nếu đã bật Docker).
- Kiểm tra logs trong `logs/` hoặc sử dụng `cw-log` để xem nhật ký runtime.

---
*File này được tạo để hướng dẫn agent tự động cài đặt và đẩy mã. Bạn có thể chỉnh sửa đường dẫn hoặc tùy chỉnh thêm các bước nếu cần.*
