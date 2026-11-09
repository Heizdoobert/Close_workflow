#!/usr/bin/env bash
# E2E test for listener.sh v2: wait for watch sync, drop task, verify
cd "$(dirname "$0")" || exit 1
RESULT=/tmp/listener_e2e_result.txt
OUT=/tmp/listener_e2e_out.log
: > "$RESULT"
rm -f incoming_tasks/e2e_listener_test.txt incoming_tasks/processed/e2e_listener_test.txt

nohup ./listener.sh > "$OUT" 2>&1 &
LPID=$!

# sync: wait until poll loop banner (max 10s)
for i in $(seq 1 20); do
  grep -q "Listener đang chạy" "$OUT" && break
  sleep 0.5
done
grep -q "Listener đang chạy" "$OUT" || { echo "FAIL: listener never started" >> "$RESULT"; kill $LPID; cat "$RESULT"; exit 1; }
echo "$(date +%T) poll loop ready, dropping file" >> "$RESULT"

echo "tôi có task cho bạn:[listener.sh e2e]" > incoming_tasks/e2e_listener_test.txt

for i in $(seq 1 18); do
  sleep 5
  [ -f "incoming_tasks/processed/e2e_listener_test.txt" ] && break
done

if [ -f "incoming_tasks/processed/e2e_listener_test.txt" ]; then
  echo "PASS: file processed and archived $(date +%T)" >> "$RESULT"
else
  echo "FAIL: not processed (incoming: $(ls incoming_tasks/*.txt 2>/dev/null | tr '\n' ' '))" >> "$RESULT"
fi

LOG=$(ls -t logs/e2e_listener_test_*.log 2>/dev/null | head -1)
if [ -n "$LOG" ] && grep -q '"validation_pass": true' "$LOG"; then
  echo "PASS: workflow log validation_pass=true ($LOG)" >> "$RESULT"
elif [ -n "$LOG" ]; then
  echo "FAIL: log exists but validation not true ($LOG)" >> "$RESULT"
else
  echo "FAIL: no workflow log produced" >> "$RESULT"
fi

echo "--- listener stdout:" >> "$RESULT"
cat "$OUT" >> "$RESULT"

kill "$LPID" 2>/dev/null
pkill -f "inotifywait.*incoming_tasks" 2>/dev/null
cat "$RESULT"
