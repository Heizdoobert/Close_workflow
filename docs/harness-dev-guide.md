# FLOW harness + AHE: tài liệu cho dev (ngày 2026-11-17)

Dành cho người sẽ sửa hoặc vận hành FLOW. Mỗi mục ghi nguồn. Viết ở commit `72d423a`, cập nhật đến `3a86de8` (sau pha 1-3 của `harness-plan`). Số dòng `file:N` là số dòng lúc viết và có thể đã lệch; tên hàm vẫn đúng.

## 0. Mức tin cậy

- Ba agent đọc code (engine/plan flow, tool/quyền/sandbox/gate, bench). Không agent nào chạy test hay backend. Số dòng là số dòng khi đọc.
- Tôi tự kiểm lại (lúc viết): `BASH_ALLOW`, `--setting-sources project`, danh sách mount của bwrap, env của agent không bị lọc (pha 1 đã sửa), env của acceptance bị lọc, luật `stuck` sau 3 lần, gate `tests` bỏ qua khi thiếu pytest.
- Các mục "chưa kiểm" ghi rõ ở từng chỗ. AHE lấy từ bài báo [arXiv 2604.25850](https://arxiv.org/html/2604.25850v2), agent chưa đọc Appendix C/D/E.

## 1. Bức tranh chung

```
flow-task (CLI, flow_engine/cli.py)
 ├─ task đơn:  enqueue -> incoming_tasks/ -> listener -> engine.execute -> logs/<id>.md
 └─ plan:      plan -> (bạn) approve -> runner -> step agents trong worktree
                 -> gates -> merge vào flow/<id> -> review, SHIP -> report.md
bench (flow_engine/bench.py): run -> compare -> analyze -> change   (vòng AHE)
```

- Engine là package stdlib `flow_engine/` (22 module cộng `__init__` và `__main__`). `engine` và `plan` import lẫn nhau lazily, `agents`->`api`, `sandbox`->`engine`, `journal`->`plan` cũng lazily.
- `ROOT` (engine.py:15) = `FLOW_HOME` hoặc thư mục cha của package. `plans/`, `logs/`, `incoming_tasks/` nằm dưới `ROOT`. **Bản deploy** (`~/Pixi Projects/Close_workflow`, không phải git) có `ROOT` riêng, nên plan tạo bởi bản repo và bản deploy không thấy nhau (bài học từ plan 8969).
- Plan nhắm repo của cwd người gọi (hoặc `FLOW_WORKDIR`) và lưu vào `plan.json` (plan.py:69-79, 311). Approve từ thư mục khác không đổi repo đích.

## 2. Vòng đời

### Task đơn (engine.py)
1. `submit`: Redis `task:<time_ns>`, TTL 30 ngày (48-56).
2. `enqueue`: ghi `incoming_tasks/<id>.txt` bằng tmp + `os.replace` (59-66).
3. `listen`: một listener nhờ flock `.listener.lock`, 2 giây quét một lần, cũng gọi `plan.resume_stale` (254-270).
4. `execute`: recall bộ nhớ, `agents.call` với `READ_TOOLS` (mặc định chỉ đọc), tùy chọn gate khi `FLOW_GATE=1`, ghi toàn bộ câu trả lời vào `logs/<id>.md`, Redis giữ 4000 ký tự (116-188).
5. Trạng thái: `queued`, `running`, `done`, `failed`, `unknown`.

Hệ quả cho dev: task đơn **chỉ đọc** (tools `Read,Grep,Glob` trừ khi `FLOW_TOOLS`), nên agent trả lời tốt nhưng không sửa file. Muốn sửa file thì dùng `plan`.

### Plan (plan.py)
Thư mục `plans/<pid>/`: `plan.json`, `analysis.md`, `plan.md`, `state.json`, `STATUS.md`, `run.log`, `run.lock`, `report.md`, `report.json`, `journal/<sid>.jsonl`, `wt/_plan`, `wt/<sid>`. Nhánh `flow/<pid>` (kết quả) và `flowstep/<pid>/<sid>` (từng step).

| Trạng thái | Khi nào |
|---|---|
| `awaiting_approval` | sau `create` |
| `rejected` | `reject`, chỉ từ `awaiting_approval` |
| `approved` | `approve`, hoặc `retry` |
| `running` | runner bắt đầu |
| `done`, `partial`, `failed` | kết thúc; `failed` cũng khi hash đổi, lỗi khởi động, vượt `FLOW_MAX_RESUMES` |

Chuỗi: `create` (cần repo có HEAD, ghi file dirty) -> `approve` (từ chối cây bẩn trừ `--allow-dirty`, đóng băng `plan_hash`) -> `run` (kiểm hash, flock, tạo `wt/_plan`, chạy step song song bằng `FLOW_PARALLEL`=3, heartbeat 5 giây) -> `integrate` -> các pass phụ (security, scout, review, verify, claims, ship) -> `report.md`.

Mỗi step (`do_step`, plan.py:700-831): worktree từ HEAD của plan, tối đa `FLOW_RETRIES`+1 lần thử, mỗi lần chạy acceptance rồi gate. Qua thì commit và `merge --no-ff` vào `flow/<pid>`. Xung đột merge được thêm một lần chạy cứu.

**Luật stuck:** cùng một dấu vết lỗi (đã bỏ chữ số) lặp 3 lần thì dừng step (plan.py:768-773). Plan bị kẹt vì acceptance sai (như `f5e5`) sẽ luôn chết kiểu này.

`recover`/listener: plan `running` hoặc `approved` mà heartbeat quá `FLOW_STALE` (120 giây) thì được resume, cộng `resumes`.

## 3. Lệnh `flow-task`

| Lệnh | Việc |
|---|---|
| `"<msg>"` (từ lạ) | chỉ lưu Redis; chạy khi `FLOW_EXEC=1` |
| `enqueue "<msg>" [--model]` | lưu và xếp hàng cho listener |
| `exec <id> <msg>` | chạy task đã lưu, đồng bộ |
| `status <id>` | state plan nếu có, ngược lại state task |
| `plan "<goal>" [--model] [--brief F --confirmed]` | tạo plan |
| `where [<plan>]`, `show`, `report`, `plans` | xem trạng thái |
| `approve <plan> [--wait] [--allow-dirty]`, `reject`, `retry`, `run` | điều khiển |
| `recover`, `clean [--older-than N] [--result-branches]` | dọn và hồi phục |
| `gate [dir]` | chạy gate, in JSON |
| `interview`, `interview-step`, `selftest` | phỏng vấn, tự kiểm |
| `doctor` | sức khỏe: bắt buộc python, `REDIS_PASSWORD`, redis, claude |
| `bench run/compare/analyze/change` | vòng AHE |

Bẫy: `plan` không có mục tiêu, hoặc `where` kèm nhiều hơn một từ, bị lưu thành task thường (cli.py:110, 129, 188). Dùng đúng cú pháp.

## 4. Tool và quyền

| Backend | Quyền |
|---|---|
| claude | `--tools` theo vai trò. Có Edit/Write/Bash thì thêm `--permission-mode acceptEdits --allowedTools BASH_ALLOW` (agents.py:259-262) |
| agy | không có cờ tool; `--mode accept-edits` chỉ khi có tool ghi; `READ_TOOLS` **không được ép** |
| opencode | không có cờ tool hay quyền; `NO_SANDBOX=1`; budget không dùng |
| api | prompt-only, không tool; chỉ https (http cho localhost) |

- `READ_TOOLS` = `Read,Grep,Glob`; `WRITE_TOOLS` thêm `Edit,Write,Bash` (agents.py:33-34).
- `BASH_ALLOW` mặc định: `python3`, `python`, `pytest`, `git status`, `git diff`, `ls`, `cat` (agents.py:35, ghi đè bằng `FLOW_BASH_ALLOW`). **`python3:*` tương đương chạy code tùy ý.**
- Dòng lệnh claude: `claude -p --model M --output-format json|stream-json [--verbose] --no-session-persistence --max-budget-usd B --setting-sources project --strict-mcp-config --disable-slash-commands --tools T`, prompt qua stdin.

| Vai trò | Tool | prompt_only | Timeout | Budget |
|---|---|---|---|---|
| planner | READ | không | 300s | `FLOW_PLAN_USD` 0.30 |
| map | READ | không | 300s | `FLOW_MAP_USD` 0.10 |
| scout | READ | không | 300s | `FLOW_SCOUT_USD` 0.10 |
| step (code) | WRITE | không | `FLOW_TIMEOUT` 600 | dự phòng mỗi step `FLOW_STEP_USD` 0.40 |
| review | READ | có | 300s | `FLOW_REVIEW_USD` 0.15 |
| claims LLM | READ | không | 300s | `FLOW_CLAIMS_USD` 0.2 |
| SHIP personas | READ | có | 300s | `FLOW_SHIP_USD` 0.08 mỗi persona |
| interview | READ | có | 300s | `FLOW_INTERVIEW_USD` 0.10 |
| task đơn | READ | không | 600s | `FLOW_MAX_USD` 0.50 |

Ngân sách: claude bị chặn cứng bởi `--max-budget-usd`. agy chỉ ước tính sau khi gọi. opencode không kiểm. api giới hạn `max_tokens` trước khi gửi. Ngân sách cả plan `FLOW_PLAN_MAX_USD` 3.0; ngân sách token `FLOW_PLAN_MAX_TOKENS` mặc định tắt.

## 5. Sandbox (sandbox.py)

- Chỉ dùng khi bwrap chạy được một lần thăm dò. Lệnh: `--ro-bind / /`, `--dev /dev`, `--proc /proc`, `--bind /tmp`, `--bind <worktree>`, cộng `~/.claude` và `~/.claude.json` ghi được (28-41).
- **Ghi được:** worktree, `/tmp`, `~/.claude`, `~/.claude.json`. Mọi thứ khác chỉ đọc.
- **Bị che (pha 1):** `SECRET_DIRS` (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.kube`, `~/.gemini`, `~/.config/gh`, `~/.config/gcloud`) thành tmpfs rỗng; `SECRET_FILES` và `.env` của engine bị `/dev/null` đè nên mở ra là "Permission denied". Đường dẫn nằm trong worktree không bị che. `~/.claude` vẫn thấy.
- **Ký commit (pha 3):** `_no_signing` bind bản sao `~/.gitconfig` (hoặc `~/.config/git/config`) có thêm `commit.gpgsign = false` và `tag.gpgsign = false` đè lên file gốc. Bản sao nằm ở `<FLOW_HOME>/logs/sandbox-gitconfig-<hash>`, quyền 0600. Không có nó, mọi `git commit` trong sandbox fail vì khóa ký nằm trong `~/.ssh` đã bị che.
- **Mạng:** không có `--unshare-net`. Agent ra mạng tự do.
- **Env (pha 1):** agent nhận `secrets.agent_env()`: bỏ mọi tên khớp `SECRET_ENV`, trừ `KEEP_AUTH` (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`) và tên trong `FLOW_AGENT_ENV_KEEP`. Acceptance cũng lọc `SECRET_ENV`.
- Thiếu bwrap: chạy không bọc, cảnh báo một lần. agy và opencode không bao giờ được bọc.
- `escaping_paths` (plan.py:144-155) chỉ kiểm đường dẫn đã commit mà ra ngoài worktree. Không phát hiện ghi ở nơi khác, dù docstring `sandbox.py:5` nghe như có.

## 6. Gate

Mỗi lần thử step: acceptance trước (plan.py:752), rồi `gate.run_gates(wt, allowed=step.files)` (754).

| Gate | Kiểm | Fail khi | Nguồn |
|---|---|---|---|
| secrets | gitleaks quét thư mục nếu có, không thì regex trên dòng thêm và tên file bí mật | có finding | gate.py:182-192 |
| syntax | `py_compile` cho `.py`, `bash -n` cho `.sh` | rc != 0 | 194-197 |
| tests | `pytest -q -x` trên host, 600s | fail. **Pass khi thiếu pytest hoặc không có test** | 199-206 |
| constraints | thêm marker skip/xfail, mất định nghĩa test, xóa file test, giảm assert, đụng config gitleaks | bất kỳ | 48, 81-100 |
| blast_radius | xóa hoặc đổi tên không khai báo; def top-level bị xóa vẫn được dùng | bất kỳ | 150-168 |
| security | 9 regex (eval/exec, `shell=True`, pickle, `verify=False`, SQL nối chuỗi, mktemp, chmod 777, cookie, innerHTML) trên dòng thêm | trúng loại cứng | audit.py:18-28 |
| code_verify | trích dẫn `file:line` và `name==version` trong Markdown, pdb, `except` trống, test không assert, hàm stub, file khai báo thiếu | trúng loại cứng | audit.py:129-199 |
| critic | agent-system trên từng `.py` | chỉ khi `FLOW_CRITIC=1` | gate.py:215-217 |

Sau tích hợp (chỉ cố vấn): `claims` chạy lại acceptance ở commit từng step và so với dòng `CLAIM:`; một pass LLM đọc analysis; review; scout; SHIP gồm 3 persona, `method.decide` gộp thành GO/NO-GO (không tự merge).

Lỗ gate: `tests` coi như qua khi thiếu pytest; kiểm tên file bí mật bị bỏ khi có gitleaks; `blast_radius` không nhìn chữ ký hàm; không gate nào nhìn `.claude/`.

**Acceptance** do planner viết và chạy bằng bash trên host, không bọc sandbox (plan.py:595-606). Chỉ kiểm "tầm thường" (159-170). Bạn xem nó khi approve. Đó là chỗ plan hỏng thầm lặng nhất: acceptance sai thì step không bao giờ qua, hoặc luôn qua.

## 7. Chính sách model và backend

- `FLOW_BACKENDS` thắng tất cả. Không thì `policy_chain` (agents.py:420-430):
  - cha là GPT-OSS-120B: `agy:<cha>!` (ghim, không bao giờ fallback);
  - cha là Gemini Flash, vai trò code: `agy:gpt-oss-120b-medium!`; vai trò chung: `agy:<cha>` rồi `agy:gemini-3.8-flash-low`;
  - còn lại: `claude:haiku` (task đơn dùng `FLOW_MODEL`).
- Lỗi tạm (regex trên text lỗi hoặc timeout): thử lại tối đa `FLOW_API_RETRIES`=3, lùi `min(30, 2^k)` nhân jitter, tôn trọng `Retry-After`. Ghim `!`: `FLOW_PIN_RETRIES`=10, trần 60 giây. Lỗi cứng: sang backend kế ngay. Ngân sách từ chối: dừng chuỗi.
- Đồng thời: `FLOW_MAX_AGENTS`=4 (semaphore + file lock).
- Circuit breaker chỉ có ở `api.py` (5 lỗi tạm liên tiếp, nghỉ 60 giây, trạng thái trong tiến trình).
- Truyền model id của chính bạn khi tạo plan (`--model`): flow-task không tự phát hiện.

## 8. Biến môi trường chính

Đủ bảng ở các file `plan.py`, `engine.py`, `agents.py`. Hay dùng:

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `FLOW_HOME` | thư mục cha package | gốc `plans/`, `logs/`, `incoming_tasks/` |
| `FLOW_WORKDIR` | cwd / git toplevel | repo đích |
| `FLOW_EXEC` | tắt | `1` chạy task ngay sau khi lưu |
| `FLOW_CLAUDE` | `~/.local/bin/claude.real` rồi PATH | binary claude |
| `FLOW_BACKENDS` | trống | chuỗi backend tường minh |
| `FLOW_REQUIRE_MODEL` | tắt | `1` từ chối khi không nêu model |
| `FLOW_RETRIES` | 2 | số lần thử lại step và task |
| `FLOW_PARALLEL` | 3 | số step song song |
| `FLOW_ACCEPT_TIMEOUT` | 300s | timeout acceptance |
| `FLOW_STALE`, `FLOW_MAX_RESUMES` | 120s, 10 | hồi phục plan chết |
| `FLOW_SHIP`, `FLOW_REVIEW`, `FLOW_SCOUT`, `FLOW_MAP`, `FLOW_CLAIMS` | 1 | bật hoặc tắt từng pass |
| `FLOW_PLAN_MAX_USD`, `FLOW_STEP_USD` | 3.0, 0.40 | ngân sách |
| `FLOW_BENCH_BACKENDS`, `FLOW_BENCH_USD`, `FLOW_BENCH_UNSANDBOXED` | `claude:haiku`, 0.50, tắt | bench |

## 9. Bench: tham chiếu

### Lệnh và tệp
| Lệnh | Đọc | Ghi |
|---|---|---|
| `run` | `harness/bench/*/{task.md,accept.sh}`, file đã track | `harness/runs/N/results.json`, `trace/<task>.jsonl` |
| `compare` | `runs/N` và `N-1`, `manifest/N.json` | `runs/N/eval.json`; khôi phục file nếu luật kích hoạt |
| `analyze` | `runs/N/results.json`, trace | chỉ stdout |
| `change --file F (1-2 lần) --evidence --root --fix --predict T1,T2\|none [--risk T1,T2]` | file component, thư mục task | `harness/manifest/N.json` |

Lệnh nào cũng in dòng cuối `NEXT: <lệnh>`. `run` thoát 0 kể cả khi task fail.

### Luật
- **Run đầy đủ:** `results.json` ghi trong `finally`, `complete` = mọi task đã xong. `compare` và `analyze` bỏ qua run dở (bench.py:67-75, 88-90). N của `run` và `change` = 1 + số lớn nhất kể cả run dở.
- **Reward:** `accept.sh` thoát 0 thì 1, còn lại hoặc timeout 300s thì 0.
- **`accept.sh`:** `bash <đường dẫn tuyệt đối>` trong bản copy, env chỉ `PATH`, `HOME`=tmp, `LANG`. Bọc bwrap nếu có. Bench **cần bwrap** trừ khi `FLOW_BENCH_UNSANDBOXED=1`.
- **Agent bench:** tool `Read,Edit,Write,Bash,Glob,Grep`, budget `FLOW_BENCH_USD`, timeout 600, env đã lọc như agent plan (`secrets.agent_env`). Danh sách tool không chặn được việc ghi: pha 2 thấy agent ghi file bằng `python3 - <<EOF` và `printf ... >` khi không có `Edit`, `Write`.
- **File được copy:** chỉ `git ls-files`, bỏ `harness/`, symlink, `.env*` (trừ `.env.example`, `.env.sample`, `.env.template`), `.npmrc`, `.netrc`, `id_*`, `*.pem/*.key/*.p12/*.pfx`.
- **`compare`:** flip = 0→1, regression = 1→0. Khôi phục khi có manifest **và** (regression > flip **hoặc** `--predict` có tên mà không task nào trong đó flip). `--predict none` chỉ giữ luật regression. Không manifest thì không bao giờ khôi phục. In thêm tổng cost và tổng turn của hai run (`turns n/a` khi run cũ chưa ghi turn), tách regression dự đoán (`--risk`) và bất ngờ, cảnh báo khi có run mới hơn còn dở. Kiểm đường dẫn trước khi ghi: không tuyệt đối, không `..`, trùng đúng một trong 22 file component, không symlink ở đường dẫn hay thư mục cha, nằm trong cwd.
- **`change`:** bốn trường không rỗng, 1-2 file, mỗi file qua whitelist 22 file, không symlink, tên trong `--predict` và `--risk` phải là thư mục task, từ chối nếu `manifest/N.json` đã có. Snapshot = **nội dung file lúc chạy `change`**, nên phải chạy `change` **trước** khi sửa file; dòng cuối là `NEXT: edit the files, then: flow-task bench run`.
- **`analyze`:** với mỗi task fail, lấy event tool của turn cuối. Có lệnh bị từ chối (`denied`) ở turn đó thì class `backends`; không thì class của file đầu tiên bị chạm (kể cả đọc), không có thì `unclassified`. Nhãn `[turn N]`, thêm `, D denied` khi task có lệnh bị từ chối (D đếm trên cả task). Khi mọi task pass, báo bench bão hòa.
- **`bench.py` không nằm trong 22 file:** cố ý, để vòng lặp không tự sửa thước đo của chính nó.

### Định dạng
```
results.json  {"run":1,"complete":true,"tasks":{"greet":{"reward":1,"accept_exit":0,"cost_usd":0.003,"tokens":{...},"ok":true,"error":"","backend":"claude:haiku","turns":6,"denied":1}}}
trace/<t>.jsonl  {"kind":"attempt",...}  {"kind":"tool","step":0,"turn":1,"tool":"Bash","file":"","cmd":"...","error":"This command requires approval","denied":true}  {"kind":"accept","exit":0,"out":""}
eval.json     {"run":2,"improvements":0,"regressions":0,"predicted_flip":0,"restored":["flow_engine/agents.py"],"reason":"...","cost":[0.031,0.0298],"turns":[38,54],"regressions_expected":[],"regressions_unexpected":[]}
manifest/N    {"run":2,"files":[...],"evidence":"...","root":"...","fix":"...","predict":["greet"],"risk":[],"snapshot":{"<file>":"<toàn bộ text>"}}
```
Event `Bash` có thêm `cmd` đã che secret (`secrets.redact` rồi cắt 120 ký tự). Event nào có tool result lỗi thì có `error` (đã che, 120 ký tự); `denied` khi lệnh nằm trong `permission_denials` của claude hoặc tool không có ("No such tool available"). `step` luôn 0 trong bench.

### Thêm task bench
Tạo `harness/bench/<tên>/task.md` (gửi nguyên văn làm prompt) và `harness/bench/<tên>/accept.sh` (thoát 0 là qua). Cần cả hai. Tên thư mục là id task trong kết quả và `--predict`. `harness/` không được copy vào repo của agent.

Chạy test: `python3 -m pytest -q tests/test_bench_*.py tests/test_agents_stream.py` (Python 3.11). Full suite: 705 test, khoảng 4 phút.

## 10. AHE gốc và bench của FLOW

Bài báo ([arXiv 2604.25850](https://arxiv.org/html/2604.25850v2)): một agent Evolve riêng sửa harness của agent code, model giữ nguyên. Mỗi vòng: chạy k rollout, làm sạch trace, chấm manifest của vòng trước (rollback nếu bị bác), Debugger tóm tắt theo tầng, Evolve sửa workspace và ghi manifest mới, commit, theo dõi bản tốt nhất.

| Mục | AHE gốc | Bench FLOW |
|---|---|---|
| 7 thành phần | `systemprompt.md`, `tool_descriptions/`, `tools/`, `middleware/`, `skills/`, `sub_agents/`, `LongTermMEMORY.md` | 7 **class file** của FLOW: prompts, planning, gates, backends, engine, state_io, cli (bench.py:20-28). Không trùng |
| Manifest | `id`, `type`, `description`, `files`, `failure_pattern`, `predicted_fixes`, `risk_tasks`, `constraint_level`, `why_this_component` | `evidence`, `root`, `fix`, `predict`, `risk` (giống `risk_tasks`, từ pha 3), `files`, `snapshot` |
| Bốn mục bắt buộc | failure evidence, root cause, targeted fix, predicted impact | khớp bốn trường trên |
| Giới hạn số file | bài **không nêu** giới hạn | tối đa 2 (luật của FLOW) |
| Phán quyết | KEEP, IMPROVE, ROLLBACK+PIVOT; rollback nếu cùng lớp lỗi dai dẳng 2+ vòng ở một tầng | chỉ rollback khi regression > flip hoặc không có flip dự đoán |
| Evolve agent | có, tự động | **không có**: người hoặc plan flow sửa |
| Artifact mỗi vòng | `iteration_NNN/` đóng băng, `change_evaluation.json`, báo cáo từng task | `runs/N/`, `eval.json`, `trace/`; không có báo cáo từng task |
| Holdout | không có (tự nhận); chọn trên cùng 89 task | không có |
| Kết quả | 69,7%→77,0% trên Terminal-Bench 2 (89 task, 10 vòng, k=2) ≈ 6,5 task | 5 task, 6 run thật (pha 2-3), bão hòa: haiku pass hết khi harness bình thường |

Độ chính xác dự đoán trong bài: dự đoán fix precision 33,7%, recall 51,4%; dự đoán regression chỉ khoảng 11-12%. Tức là chuỗi dự đoán của agent kém tin cậy, nên rollback tự động là phần giá trị chính.

Tương ứng tầng của FLOW với AHE:
- Evaluate = `bench run`; Verify and gate = `bench compare`; Analyze = `bench analyze`; Improve = `bench change` cộng plan flow thực hiện sửa.
- Trạng thái thật: pha 2 đã chạy cả bốn lệnh trên dữ liệu thật (regression, flip, cả hai luật rollback); chưa có thay đổi nào được giữ vì bench bão hòa. Chi tiết: `docs/bench-todo.md`.

## 11. Lỗ an ninh, xếp theo mức nghiêm trọng (chưa chạy thử để khai thác)

1. **Bí mật đọc được và gửi ra ngoài được.** `--ro-bind /` cho đọc toàn bộ đĩa trừ các đường dẫn bị che (mục 5), không `--unshare-net`. File bí mật nằm ngoài danh sách che, và `~/.claude`, vẫn đọc được. Cả agent plan và agent bench.
2. ~~**Agent kế thừa toàn bộ env**~~ Đã sửa ở pha 1: env của agent bỏ mọi tên khớp `SECRET_ENV` (mục 5).
3. **`python3:*` được phép** nên agent chạy code tùy ý, kể cả `git commit`. Claude còn tự cho chạy lệnh chỉ đọc nằm ngoài `--allowedTools`; một biến env đứng trước lệnh (`PYTHONPATH=. python3 ...`) thì bị từ chối. Gate so với HEAD (gate.py:42, 78, 118), commit sẽ giấu thay đổi khỏi gate. Dưới bwrap có thể bị chặn vì `.git` chỉ đọc (suy luận, chưa thử); không bwrap thì không bị chặn.
4. **Code do agent viết chạy trên host không bọc:** pytest của gate, gitleaks, critic, acceptance.
5. **`~/.claude` và `~/.claude.json` ghi được** trong sandbox; tác động lên phiên sau chưa kiểm.
6. **`READ_TOOLS` không ép với agy và opencode.** Planner, review, claims chỉ thật sự chỉ đọc trên claude.
7. **`prompt_only` chỉ api.py đọc.** Gọi claude có `prompt_only` vẫn được `Read,Grep,Glob`.
8. **`.claude/settings.json` trong worktree** được nạp (`--setting-sources project`). Một file commit sẵn có thể ảnh hưởng lần chạy sau; hook có chạy hay không chưa kiểm.
9. **Ngân sách:** opencode không giới hạn, agy chi tiêu trước khi kiểm, ngân sách token tắt mặc định.
10. **Tiêm bền vững:** phần recall bộ nhớ và recipe rút từ text lỗi của agent đi vào prompt sau; redaction chỉ che chuỗi dạng khóa.
11. **Acceptance sai** (xem mục 6) là rủi ro chất lượng hơn là bảo mật, nhưng hay gặp nhất.

## 12. Công thức làm việc

- **Thêm gate:** hàm trong `gate.py` trả `{ok, ...}`, đăng ký trong `run_gates`; thêm test; nếu gate đọc dòng thêm thì dùng `audit.py` làm mẫu; ghi vào `CLAUDE.md`. Nhớ gate chạy dưới `constraints` chống tự hạ chuẩn.
- **Thêm task bench:** mục 9; làm task nhỏ có thể fail; chạy hai lần `bench run` quanh một thay đổi.
- **Plan bị kẹt:** `flow-task where <plan>`, đọc `plans/<pid>/journal/<sid>.jsonl`; nếu 3 lần fail y hệt, nghi acceptance hoặc prompt; sửa plan mới thay vì retry mù.
- **Plan không thấy:** kiểm `FLOW_HOME`. Plan tạo bằng engine repo nằm ở `<repo>/plans`, còn bản deploy dùng `~/Pixi Projects/Close_workflow/plans`.
- **Deploy:** backup tgz (loại `.venv`, `plans`, `logs`, `incoming_tasks`), `git archive <commit>`, `rsync` không `--delete` loại `.venv`, `plans`, `logs`, `incoming_tasks`, `.env`, ghi `.deployed_commit`, `systemctl --user restart flow-listener.service`, `flow-task doctor`, một task smoke.
- **Python:** dùng `/usr/bin/python3.11`. Tránh shebang dùng `sys.executable` trên đường dẫn có dấu cách ("Pixi Projects"): test dùng wrapper `sh` có quote.

## 13. Tài liệu lỗi thời và việc nên làm

- Đã sửa ở pha 3: "7 component dirs" và trace "step-indexed" trong `CLAUDE.md`, `AGENT_SETUP.md`.
- `docs/AGENTIC_GAPS.md` không có nội dung bench, base `fd08d93` đã cũ, thân bài có mục mâu thuẫn với dòng "FIXED" của chính nó (#1, #3, #9, #12); số dòng đã lệch (#2, #7).
- `docs/bench-todo.md` là danh sách việc bench còn mở.

Việc đáng làm trước, theo thứ tự (cập nhật sau pha 3):
1. ~~Thêm task bench có thể fail, chạy hai run~~ Đã làm ở pha 2, nhưng haiku vẫn pass hết: cần 2-3 task khó hơn, chạy thật để chắc chúng fail ở baseline.
2. ~~Lọc env của agent~~ Đã làm ở pha 1.
3. Thêm `risk_tasks` vào manifest (dự đoán regression), đây là phần AHE dùng để chấm.
4. Quyết định về `step` và thư mục `harness/runs` (commit baseline hay giữ local).
5. Sửa tài liệu lỗi thời ở mục 13.
