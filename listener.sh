#!/usr/bin/env bash
# listener.sh – daemon tự động phát hiện task và gọi sub‑agent, ghi log chi tiết

WATCH_DIR="./incoming_tasks"
PROCESSED_DIR="${WATCH_DIR}/processed"
LOG_DIR="./logs"

mkdir -p "$WATCH_DIR" "$PROCESSED_DIR" "$LOG_DIR"

echo "🔔 Listener đang chạy – chờ file .txt trong $WATCH_DIR"

# Sử dụng inotifywait để nhận sự kiện tạo/đóng file
inotifywait -m -e close_write --format "%w%f" "$WATCH_DIR" |
while read -r NEWFILE; do
  # Chỉ xử lý .txt
  [[ "$NEWFILE" == *.txt ]] || continue

  # Đọc nội dung (kỳ vọng chỉ 1 dòng)
  MESSAGE=$(cat "$NEWFILE")
  echo "🚀 Phát hiện task: $MESSAGE"

  # Tạo file log cho task này (dùng tên file + timestamp)
  TS=$(date +%Y%m%d_%H%M%S)
  LOGFILE="${LOG_DIR}/$(basename "${NEWFILE%.*}")_${TS}.log"
  echo "📝 Logging chi tiết vào $LOGFILE"

  # Gọi sub‑agent qua wrapper (run_task.sh) và ghi log
  /mnt/second_drive/FLOW_SKILL/run_task.sh "$MESSAGE" 2>&1 | tee -a "$LOGFILE"

  # Di chuyển file đã xử lý
  mv "$NEWFILE" "$PROCESSED_DIR/$(basename "$NEWFILE")"
  echo "✅ Task đã hoàn thành, log lưu tại $LOGFILE" >> "$LOGFILE"
 done
