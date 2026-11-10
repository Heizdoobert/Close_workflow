# legacy/

Files from before `flow_engine/` became the engine. Nothing in the package, the tests, CI or the install scripts uses them; they are kept only so the history is easy to find.

| File | What it was | Why it is not used |
|---|---|---|
| `api_service.py` | FastAPI router for an earlier HTTP service | needs `fastapi`, which is not a dependency, and a sibling workspace that is not in this repo |
| `generated_api.py` | output of an earlier code-generation task | auto-generated stub, no callers |
| `close_workflow_cli.py` | shim to a CLI in a `D:\` workspace | hard-coded path outside the repo |
| `closed_loop_task_engine.json` | earlier engine definition | replaced by `flow_engine/` |
| `soul_canonical_15nt.md` | earlier agent notes | not read by any code |
| `test_report.md`, `test_system.sh` | earlier manual test report and shell check | replaced by `pytest` and `flow-task selftest` |

To restore one: `git mv legacy/<file> <file>`. To drop them for good, delete this folder; git history keeps everything.
