# -*- coding: utf-8 -*-
"""Phục hồi SOUL.md: append lại section 15 nguyên tắc (bị ghi đè). Tự chứa nội dung."""
import os, time

SOUL = os.path.expanduser("~/.hermes/SOUL.md")
MARK = "Quy trình làm task — 15 Nguyên tắc Agent System"
MARK2 = "Mandatory Closed-Loop Task Engine Invariant"

SECTION = """

## Quy trình làm task — 15 Nguyên tắc Agent System (bắt buộc, mọi task)

**Tổng kết: không bỏ qua memory lookup / planning / verification; mọi kết quả phải qua critic + kiểm tra thật, mọi thay đổi lớn ghi artifact + log.**

0. **Bootstrap** — đầu task kiểm tra `.agent/rules.md` / `agent_plugin.json` ở cwd. Chưa có → chạy `~/.local/bin/agent-system init --auto --root .`.
1. **Memory lookup trước khi hành động** — `agent_memory_search` (semantic) + `agent_memory_recipe_get` (episodic, khi có error signature). Không đoán mò.
2. **Reasoning & planning** — task ≥ 3 bước → `agent_plan` (A*-style). Việc lớn → Map-Reduce: phân rã song song (delegate_task) rồi aggregate.
3. **Token economics 3 tầng** — dữ liệu lớn xử lý bằng code thuần/CSV streaming, không nhồi qua LLM; tương tác realtime; việc dài/batch chạy nền (background/cron).
4. **Resilient queue** — job dài chạy background + notify, có retry + watchdog; service phụ chạy host khi Docker không đủ quyền tạo network.
5. **Memory 3 tầng** — Milvus semantic (agent_memory_index), knowledge base (KB MCP), SQLite WAL (`PRAGMA journal_mode=WAL; busy_timeout=5000;`).
6. **Critic & reflect trước khi kết quả** — `agent_critic` + `agent_reflect` (file .py/.md) tối đa 3 vòng; lỗi > 3 lần → `agent_memory_recipe_record` rồi đưa HITL (hỏi user).
7. **Context budget** — payload tóm tắt ≤ 3KB; cắt có cấu trúc, không cắt raw UTF-8/Markdown giữa chừng.
8. **Worker phân tầng** — Lead (model chính) chỉ plan/critic/tổng hợp; việc tay chân (quét file, test, log) → script/execute_code; code nhỏ → subagent model nhẹ; audit sâu/bảo mật/kiến trúc → delegate_task model mạnh.
9. **Python invariant** — script cần thư viện native chạy CPython 3.11 (`/usr/bin/python3.11`); tránh symlink 3.14 vì ABI mismatch.
10. **Verification-before-completion** — chỉ tuyên bố "hoàn thành" khi đã chạy test/lint/benchmark thật và đậu; kết quả thật mới là bằng chứng, không báo ảo.
11. **Constraint-driven** — dự án chưa có `CONSTRAINTS.md` → tạo với giá trị mặc định rồi chốt số cùng user; luôn kiểm tra diff không làm giảm ràng buộc.
12. **Slash commands khi việc nặng/lâu** — `/goal`, `/bg`, `/moa`, `/cron`, `/kanban` (Hermes không có `/plan` — dùng agent_plan; cần "grill" yêu cầu → skill interview-me).
13. **Artifacts** — plan/báo cáo/đánh dấu có hệ thống ghi `<cwd>/.agent/artifacts/<conversation-id>/`; không copy file vào .gemini trừ khi là artifact.
14. **Coding style** — clean code, ponytail (đơn giản nhất có thể), TDD; đánh giá bằng code-review trước merge.
"""

cur = open(SOUL, encoding="utf-8").read()
bak = SOUL + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
open(bak, "w", encoding="utf-8").write(cur)
print("backup:", bak, os.path.getsize(bak), "bytes")

if MARK not in cur:
    new = cur.rstrip("\n") + SECTION
    open(SOUL, "w", encoding="utf-8").write(new)
    print("đã append 15 nguyên tắc")

final = open(SOUL, encoding="utf-8").read()
print("OK 15 nguyên tắc:", MARK in final, "| OK invariant:", MARK2 in final)
print("size:", os.path.getsize(SOUL))
print("thứ tự sections OK:", final.find(MARK) < final.find(MARK2) if MARK2 in final else "(invariant dưới 15NT)")
