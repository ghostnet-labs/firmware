#!/usr/bin/env bash
# Prints why an OpenWrt build failed, at the end of the job log.
#
# With make -j the failing command's output is buried thousands of lines
# above the final "Error 2", among the output of packages that kept
# building, and job log tails cut it off. This prints, for each package that
# failed, the lines before its first make error, and raises one annotation
# per package so the cause shows on the check run itself.
#
# Usage: summarize-build-failure.sh BUILD_LOG [CONTEXT_LINES]
set -uo pipefail

log="$1"
context="${2:-60}"
[ -f "$log" ] || exit 0

echo "::group::Why the build failed"
mapfile -t failed < <(grep -oE 'ERROR: (package|target|tools|toolchain)/[^ ]+ failed to build' "$log" |
  awk '{print $2}' | sort -u)
if [ "${#failed[@]}" -eq 0 ]; then
  echo "No 'failed to build' line found; showing the first make errors."
fi
for pkg in "${failed[@]}"; do
  echo "::error title=${pkg} failed to build::See 'Why the build failed' at the end of the Build firmware step."
done
# The first errors from sub-makes (make[3] and deeper) are where packages
# fail; make[1] and make[2] only report that a sub-make failed. Errors from
# the packages that failed come first: with -j, packages that recovered
# (configure probes, optional tools) print errors earlier in the log.
errors="$(grep -nE '^make\[[3-9][0-9]*\]: \*\*\* .*Error [0-9]+$' "$log" | cut -d: -f1)"
pick() {
  local line next
  for pkg in "${failed[@]}"; do
    for line in $errors; do
      # make prints "Leaving directory '<package dir>'" right after the error.
      next="$(sed -n "$((line + 1))p" "$log")"
      case "$next" in
        *"Leaving directory"*"/${pkg##*/}'"*) echo "$line"; break ;;
      esac
    done
  done
  echo "$errors"
}
pick | awk 'NF && !seen[$0]++' | head -5 |
while read -r line; do
  start=$(( line > context ? line - context : 1 ))
  echo "====== build.log lines $start-$line ======"
  sed -n "${start},${line}p" "$log"
done
echo "::endgroup::"
for pkg in "${failed[@]}"; do
  echo "Failed: $pkg"
done
