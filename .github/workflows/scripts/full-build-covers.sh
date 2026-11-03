#!/usr/bin/env bash
# Exits 0 when a successful "Full firmware build" covers commit SHA, and
# prints that run's URL. A run covers SHA when it built SHA itself, or built
# an earlier commit of SHA's history and everything changed since then is a
# CI file under .github/ other than build-firmware.yml (so the boards would
# build the same firmware). Exits 1 when no run covers SHA.
#
# Usage: full-build-covers.sh SHA
# Env: GH_TOKEN, REPO (owner/name)
set -euo pipefail

sha="$1"
# Paths that do not change what a board build produces.
ci_only='^\.github/'
build_inputs='^\.github/workflows/build-firmware\.yml$'

while read -r run_sha url; do
  if [ "$run_sha" = "$sha" ]; then
    echo "$url"
    exit 0
  fi
  # status is "ahead" when run_sha is an ancestor of sha. GitHub lists at
  # most 300 files; a diff that large is never CI-only.
  compare=$(gh api "repos/$REPO/compare/$run_sha...$sha" \
    --jq '[.status, (.files | length)] | @tsv' 2>/dev/null) || continue
  read -r status nfiles <<<"$compare"
  [ "$status" = "ahead" ] || continue
  [ "$nfiles" -lt 300 ] || continue
  changed=$(gh api "repos/$REPO/compare/$run_sha...$sha" --jq '.files[].filename')
  if [ -z "$(grep -Ev "$ci_only" <<<"$changed" || true)" ] &&
     [ -z "$(grep -E "$build_inputs" <<<"$changed" || true)" ]; then
    echo "$url"
    exit 0
  fi
done < <(gh api "repos/$REPO/actions/workflows/build-full.yml/runs?status=success&per_page=10" \
  --jq '.workflow_runs[] | "\(.head_sha) \(.html_url)"')
exit 1
