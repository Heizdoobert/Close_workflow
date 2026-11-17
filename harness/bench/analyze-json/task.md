Add a `--json` option to `flow-task bench analyze` (flow_engine/bench.py, cmd_analyze).

With `--json`, cmd_analyze prints exactly one JSON object on one line, then the usual `NEXT: ...` line as the last line:
{"run": N, "failing": {"<task>": {"class": "<class>", "turn": <int>}}}
- N is the run that was analyzed.
- "failing" maps every failing task to the class and turn that the text output already computes for it.
- With no failing task it prints {"run": N, "failing": {}} and then the NEXT line.
Without `--json` the output stays exactly as it is now. Keep the change small and do not edit other files.
