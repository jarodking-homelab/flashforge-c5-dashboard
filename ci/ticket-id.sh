#!/bin/sh
# Every commit on a branch names its tracker ticket (hl-xxx), so a line of code can be traced
# to the reason it exists. Checks the commits that are on this branch and not on main.
# Merge commits are skipped. Commits from before the rule (2026-11-17) are skipped.
set -eu
RULE_START=2026-11-17
base=${1:-origin/main}

missing=0
for sha in $(git rev-list --no-merges --since="$RULE_START" "$base..HEAD"); do
  if ! git log -1 --format=%B "$sha" | grep -Eq '\bhl-[a-z0-9]+(\.[0-9]+)*\b'; then
    echo "No ticket id: $(git log -1 --format='%h %s' "$sha")"
    missing=$((missing + 1))
  fi
done
checked=$(git rev-list --no-merges --since="$RULE_START" --count "$base..HEAD")
echo "$checked commit(s) checked, $missing without a ticket id"
[ "$missing" -eq 0 ]
