#!/usr/bin/env bash
# Prints one hash of everything a board build reads from this repository:
# every tracked file except CI files under .github/, plus the board build
# workflow itself (build-firmware.yml), which does change the build.
#
# Two commits with the same hash build the same firmware, so CI can reuse an
# earlier build instead of spending hours rebuilding after a CI-only change.
# Feeds are pinned to commits in feeds.conf.default, which this hash covers.
#
# Usage: build-inputs-hash.sh [REV]   (default HEAD)
# Needs only tree objects, so a blob-less checkout is enough.
set -euo pipefail

rev="${1:-HEAD}"
git ls-tree -r --full-tree "$rev" |
  awk -F '\t' '$2 !~ /^\.github\// || $2 == ".github/workflows/build-firmware.yml"' |
  git hash-object --stdin
