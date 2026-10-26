#!/usr/bin/env bash
# Turns on the repo security settings and applies the "Protect main" ruleset.
# Run once after creating a repo from this template:
#
#   .github/setup-repo.sh owner/name
#
# Safe to re-run: it updates the ruleset in place instead of adding another.
set -euo pipefail

repo=${1:?usage: setup-repo.sh owner/name}
dir=$(cd "$(dirname "$0")" && pwd)

# The ruleset requires the jobs in security.yml. Without that workflow the
# checks never report and every PR is blocked, so refuse instead.
if ! gh api "repos/$repo/contents/.github/workflows/security.yml" --silent 2>/dev/null; then
  echo "$repo has no .github/workflows/security.yml on its default branch; not applying the ruleset." >&2
  exit 1
fi

gh api -X PUT "repos/$repo/vulnerability-alerts" --silent
gh api -X PUT "repos/$repo/automated-security-fixes" --silent
echo "Dependabot alerts and security updates: on"

# Secret scanning, push protection and private vulnerability reporting are
# free only for public repos; personal accounts cannot enable them on private ones.
# Compare against "public": an org's internal repos also report private=false.
if [ "$(gh api "repos/$repo" --jq .visibility)" = "public" ]; then
  gh api -X PATCH "repos/$repo" --silent --input - <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}}}
JSON
  echo "Secret scanning and push protection: on"
  gh api -X PUT "repos/$repo/private-vulnerability-reporting" --silent
  echo "Private vulnerability reporting: on"
fi

id=$(gh api "repos/$repo/rulesets" --jq '.[] | select(.name == "Protect main") | .id')
if [ -n "$id" ]; then
  gh api -X PUT "repos/$repo/rulesets/$id" --input "$dir/rulesets/protect-main.json" --silent
  echo "Ruleset \"Protect main\": updated"
else
  gh api -X POST "repos/$repo/rulesets" --input "$dir/rulesets/protect-main.json" --silent
  echo "Ruleset \"Protect main\": created"
fi
