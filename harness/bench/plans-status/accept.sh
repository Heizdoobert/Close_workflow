#!/bin/bash
# reward: flow-task plans --status filters by state.json status; plain plans still lists all
set -euo pipefail
repo="$PWD"
home="$(mktemp -d)"
for spec in "p-done:done:goal one" "p-fail:failed:goal two"; do
  IFS=: read -r id status goal <<<"$spec"
  mkdir -p "$home/plans/$id"
  printf '{"id": "%s", "status": "%s", "goal": "%s"}\n' "$id" "$status" "$goal" > "$home/plans/$id/state.json"
done
cd "$home"
only="$(FLOW_HOME="$home" PYTHONPATH="$repo" python3 -m flow_engine plans --status done)"
all="$(FLOW_HOME="$home" PYTHONPATH="$repo" python3 -m flow_engine plans)"
grep -q "p-done" <<<"$only"
if grep -q "p-fail" <<<"$only"; then echo "--status done also listed p-fail" >&2; exit 1; fi  # a bare `! cmd` does not trip set -e
grep -q "p-done" <<<"$all"
grep -q "p-fail" <<<"$all"
