# Hướng dẫn cài đặt Close‑Workflow trên Windows

> **Mục tiêu**: Thiết lập nhanh chóng môi trường Closed‑Loop Task Engine (Claude, Redis, Loki + Grafana) trên Windows và chuẩn bị repo để đẩy lên GitHub.

## 1. Yêu cầu trước khi cài đặt
1. **Git** – cài đặt từ https://git-scm.com/download/win và cấu hình credential (HTTPS token hoặc SSH key).  
2. **PowerShell 5+** – có sẵn trên Windows.
3. **Docker Desktop** – tải và bật (cần bật *Expose daemon on tcp://localhost:2375* nếu muốn dùng `docker-compose` từ PowerShell).  
4. **Python 3.12+** – cài đặt từ https://www.python.org/downloads/windows/ (đánh dấu *Add Python to PATH*).
5. **Antigravity CLI** – đã có trong môi trường (`agy`), không cần cài thêm.

## 2. Chuẩn bị thư mục dự án
```powershell
cd D:\04_Tools\Workflow\Close_workflow
# Đảm bảo mọi script có line‑ending Windows
git add -A
```

## 3. Tạo môi trường ảo và cài phụ thuộc (nếu có)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # kích hoạt môi trường
# (Không có phụ thuộc Python riêng, chỉ cần Antigravity đã cài)
```

## 4. Khởi động Docker stack (Redis, Loki, Grafana)
```powershell
# Docker Desktop phải đang chạy
docker-compose -f docker-compose.yml up -d          # Redis
docker-compose -f docker-compose-logging.yml up -d   # Loki + Grafana
```
> Kiểm tra bằng `docker ps` – các container `redis`, `loki`, `grafana` phải có trạng thái *Up*.

## 5. Chạy listener và định nghĩa sub‑agent
```powershell
# Start listener (daemon) – sẽ tự động tạo sub‑agent khi có task
python listener.py &   # chạy nền
# Hoặc dùng script PowerShell có sẵn
./install.ps1
```
Listener sẽ theo dõi thư mục `incoming_tasks/` và lưu trạng thái vào Redis.

## 6. Thêm task mẫu
```powershell
"tôi có task cho bạn:[Xây API quản lý sản phẩm]" > incoming_tasks/task1.txt
```
Listener sẽ tự động xử lý và log vào `logs/`; nếu bật Loki/Grafana, xem tại `http://localhost:3000`.

## 7. Đẩy toàn bộ dự án lên GitHub (force)
```powershell
git add -A
git commit -m "Add Windows install guide (INSTALL.md)"
git push -u origin main --force
```
Sau lệnh trên, repo https://github.com/Heizdoobert/Close_workflow sẽ chứa toàn bộ code và hướng dẫn cài đặt.

---
*File này được tối ưu cho Windows; nếu chạy trên Linux/macOS, thay đổi đường dẫn và dùng `bash` thay cho PowerShell.*
