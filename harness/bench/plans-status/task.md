Add a `--status <status>` option to `flow-task plans` (flow_engine/cli.py, the "plans" command).

`flow-task plans --status done` prints only the plans whose state.json "status" is exactly "done", in the same line format as now.
`flow-task plans` without the option keeps printing every plan, unchanged.
Keep the change small and do not edit other files.
