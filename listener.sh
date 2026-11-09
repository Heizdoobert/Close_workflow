#!/usr/bin/env bash
# listener.sh – daemon tự động phát hiện task và gọi sub‑agent, ghi log chi tiết
# Poll loop thay vì inotify: inotify không tin cậy trên drvfs/9p (WSL).

WATCH_DIR="./incoming_tasks"
PROCESSED_DIR="${WATCH_DIR}/processed"
LOG_DIR="./logs"
POLL_SECONDS=2

mkdir -p "$WATCH_DIR" "$PROCESSED_DIR" "$LOG_DIR"

echo "🔔 Listener đang chạy (poll ${POLL_SECONDS}s) – chờ file .txt trong $WATCH_DIR"

while true; do
  for NEWFILE in "$WATCH_DIR"/*.txt; do
    [ -e "$NEWFILE" ] || continue

    # Đọc nội dung (kỳ vọng chỉ 1 dòng)
    MESSAGE=$(cat "$NEWFILE")
    echo "🚀 Phát hiện task: $MESSAGE"

    # Tạo file log cho task này (dùng tên file + timestamp)
    TS=$(date +%Y%m%d_%H%M%S)
    LOGFILE="${LOG_DIR}/$(basename "${NEWFILE%.*}")_${TS}.log"
    echo "📝 Logging chi tiết vào $LOGFILE"

    # Gọi sub‑agent qua wrapper (run_task.sh) và ghi log
    "$(dirname "$0")/run_task.sh" "$MESSAGE" 2>&1 | tee -a "$LOGFILE"

    # Di chuyển file đã xử lý
    mv "$NEWFILE" "$PROCESSED_DIR/$(basename "$NEWFILE")"
    echo "✅ Task đã hoàn thành, log lưu tại $LOGFILE" >> "$LOGFILE"
  done
  sleep "$POLL_SECONDS"
done
