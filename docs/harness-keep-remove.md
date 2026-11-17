# FLOW trên máy này: giữ gì, bỏ gì (2026-11-17)

Đối chiếu `harness-dev-guide.md` với máy này. Tôi chưa xóa gì. Cột "Đã kiểm" ghi điều tôi tự xem hôm nay.

Ghi chú sau pha 3 (`3a86de8`): đây là bản ghi quyết định lúc viết, trước pha 0. Từ đó `config.toml` đã merge và deploy, env của agent đã được lọc (pha 1), mục 3-4 của bảng "Bỏ hoặc sửa" đã xử lý ở pha 3 (`step` giữ lại vì tên test khóa nó, tài liệu đã sửa), mục 7 thuộc pha 4. Trạng thái mới: `docs/bench-todo.md`.

## Giữ

| Mục | Lý do | Đã kiểm |
|---|---|---|
| Plan flow: `plan`, `approve`, gate, SHIP, journal, luật stuck, `retry`, `recover`, `clean` | Dùng hằng ngày; 6 plan bench đi qua đường này, gate `constraints` bắt 3 lần xóa assert | chạy thật trong phiên này |
| Backend `claude` | Backend duy nhất được bọc bwrap, có chặn ngân sách cứng và ép tool | doctor: có `claude.real`, bwrap |
| Backend `agy` | Chính sách cha Gemini / GPT-OSS cần nó | `agy` có trên PATH; hôm nay chưa gọi thử |
| bwrap, `secrets.redact`, lọc env cho acceptance và `accept.sh` | Hàng rào thật duy nhất đang có | đọc code |
| `bench run / compare / analyze / change` | Nửa "đo" của AHE; `run` đã chạy thật | `compare`, `analyze` mới thử bằng fake |
| Luật "tối đa 2 file" của `bench change` | Không có trong AHE, nhưng giữ phạm vi sửa nhỏ. Ghi rõ là luật của FLOW | đọc code |
| `run_task.sh`, `listener.sh` | Bản deploy và systemd gọi qua chúng; `tests/test_repo_hygiene.py` kiểm | đọc file |
| `docker-compose.yml`, `redis/redis.conf` | `install.py` dùng để dựng Redis; `tests/test_compose_secrets.py` kiểm | đọc file |
| API mode (`api.py`, `apiclient.py`, `apiprice.py`) | Máy này không có key nên không chạy, nhưng vô hại khi tắt và có test. Giữ, để tắt | doctor: hai key đều thiếu |
| `config.toml` (mới) | Đổi model không cần env | 625 test qua, xem dưới |

## Bỏ hoặc sửa (cần bạn quyết)

| # | Mục | Vì sao | Rủi ro khi bỏ |
|---|---|---|---|
| 1 | Backend `opencode` | `CLAUDE.md` ghi key hết hạn (401) từ 2026-11-09; không ép tool, không kiểm ngân sách, không bọc sandbox. Hôm nay tôi chưa gọi thử lại | Mất một bậc fallback. Nếu chỉ muốn tắt: đừng đưa vào `backends` |
| 2 | `docs/AGENTIC_GAPS.md` | Base `fd08d93`, không có bench, thân bài mâu thuẫn dòng FIXED của chính nó | `tests/test_docs.py` đọc file này: phải bỏ hoặc sửa cả test |
| 3 | Trường `step` trong trace bench | Luôn bằng 0; `analyze` dùng `turn` | Sửa test đi kèm |
| 4 | Dòng tài liệu sai: "7 component dirs" (`CLAUDE.md:63`, `AGENT_SETUP.md:129`), trace "step-indexed" (`AGENT_SETUP.md:126`) | Thực tế 22 file trong 7 class, nhóm theo `turn` | `tests/test_bench_docs.py` kiểm chữ trong hai file |
| 5 | `flow-task-usrlocal-v2` (untracked, gốc repo main) | Wrapper thừa trỏ về bản deploy | Kiểm `/usr/local/bin/flow-task` trước |
| 6 | `scripts/` (untracked trong worktree, có địa chỉ IP LAN viết cứng) | Không thuộc FLOW | Của bạn, tôi không biết còn dùng không |
| 7 | Nhánh kết quả đã merge: `flow/…-aa45`, `-5554`, `-162d`, `-9247`, `-e974`, `-585c` và các nhánh tạm `bench-fix*`, `config-toml` | Đã nằm trong `main` (trừ `config-toml` chưa merge) | Không. `flow-task clean --result-branches` |
| 8 | Nhánh chưa merge cũ: `flow/20261113-175905-a63e`, `flow/20261113-184447-c977` | Tôi không biết nội dung | Mất việc nếu còn cần |
| 9 | Plan cũ trong `/mnt/second_drive/FLOW_SKILL/plans` (ví dụ `20261110-*`) | Bản deploy không thấy thư mục này, dễ gây nhầm như plan 8969 | `clean --older-than` chỉ dọn worktree; xóa thư mục là mất report |

## Từ AHE: lấy gì, không lấy gì

| Lấy | Không lấy |
|---|---|
| `risk_tasks` trong manifest (dự đoán task sẽ hỏng) | Evolve agent tự sửa harness: bài báo tự gọi là prototype, dự đoán regression chỉ đúng khoảng 11-12% |
| Lọc env của agent như đã làm cho acceptance (lỗ rộng nhất) | Thư mục `iteration_NNN` đóng băng: `git` và `runs/N` đã đủ |
| 2-3 task bench có thể fail, rồi hai run liên tiếp | Bảy thành phần AHE (`systemprompt.md`, `tools/`, `middleware/`…): FLOW không có bố cục workspace đó |
| Tập holdout khi số task đủ nhiều | Debugger LLM viết báo cáo từng task: tốn token, chưa có số đo |
|  | k rollout mỗi task: để sau, khi cần giảm nhiễu |

## config.toml

Nhánh `config-toml` (commit `31db3f3`, **chưa merge, chưa deploy**). Full pytest: 625 passed.

```toml
[models]
task = "haiku"            # task đơn (enqueue / exec)          -> FLOW_MODEL
default = "claude:haiku"  # plan, interview khi không có --model -> FLOW_DEFAULT_BACKEND
bench = "claude:haiku"    # flow-task bench run                 -> FLOW_BENCH_BACKENDS
backends = ""             # chuỗi cho MỌI lệnh gọi, thắng --model -> FLOW_BACKENDS
parent = ""               # coi mọi người gọi là model này       -> FLOW_PARENT_MODEL
```

- Đặt file ở `FLOW_HOME` (bản deploy: `~/Pixi Projects/Close_workflow/config.toml`). Repo có `config.toml.example`; `config.toml` nằm trong `.gitignore` nên deploy không ghi đè.
- Thứ tự: env thật thắng, rồi `.env`, rồi file. Giá trị rỗng nghĩa là không đặt.
- Key lạ hoặc file hỏng là lỗi ngay khi chạy `flow-task`, không lặng lẽ quay về haiku.
- `--model` của người gọi (chính sách Gemini / GPT-OSS) vẫn thắng `default`. Chỉ `backends` thắng tất cả.
- `flow-task doctor` in thêm dòng `models task=… default=… bench=… backends=… parent=…`.
- Cần Python 3.11+ (bản deploy đang 3.11).
- Chưa làm: model riêng cho từng vai trò (planner, review, SHIP). Hiện chúng dùng chung `default`.
