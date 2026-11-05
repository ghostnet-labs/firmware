#!/usr/bin/env python3
"""Merge a newer OpenWrt 24.10 tree into this firmware fork.

This repository does not share git history with openwrt/openwrt (it was
squash-imported in 2025-12), so a plain `git merge` is not possible. This
script does the equivalent file by file and leaves the result staged in the
current checkout for review. See docs/openwrt-upstream-sync.md.

Rules, per path:
  * our blob is identical to some upstream version of that path
    (an old backport or an untouched file)       -> take the new upstream file
  * our blob is not any upstream version (a real local change)
                                                 -> 3-way merge, base = the
                                                    recorded upstream base, or
                                                    the upstream version
                                                    closest to ours
  * we added the path and upstream never had it  -> keep ours
  * we deleted the path on purpose (KEEP_DELETED) -> keep it deleted

Usage (from the repo root, on a clean branch cut from origin/24.10):
  git fetch https://github.com/openwrt/openwrt.git \
      openwrt-24.10:refs/remotes/upstream/openwrt-24.10 --no-tags
  python3 scripts/openwrt-upstream-sync.py upstream/openwrt-24.10

It prints the paths that still have conflict markers. Resolve them, run the
patch-application checks from the doc, update .openwrt-upstream, commit.
"""
import collections
import os
import subprocess
import sys
import tempfile

Z = "0" * 40
STATE_FILE = ".openwrt-upstream"
# Upstream files this fork removed on purpose. Never bring them back.
KEEP_DELETED = {
    ".github/FUNDING.yml",
    ".github/pull_request_template",
    ".github/workflows/coverity.yml",
    ".github/workflows/github-release.yml",
    ".github/workflows/issue-labeller.yml",
    ".github/workflows/packages.yml",
    ".github/workflows/push-containers.yml",
    ".github/workflows/toolchain.yml",
    ".github/workflows/tools.yml",
    ".vscode/settings.json",
}
# Files this fork owns even though upstream has them. `version` and
# `version.date` are rewritten by hand for each import (see the doc).
KEEP_OURS = {"version", "version.date"}


def git(*args, binary=False, check=True):
    out = subprocess.run(["git", *args], capture_output=True, check=check)
    return out.stdout if binary else out.stdout.decode()


def tree(rev):
    blobs = {}
    for line in git("ls-tree", "-r", "--full-tree", rev).splitlines():
        meta, path = line.split("\t", 1)
        mode, _, sha = meta.split()
        blobs[path] = (mode, sha)
    return blobs


def upstream_history(upstream, since):
    seen = collections.defaultdict(set)
    raw = git("log", "--no-renames", "--raw", "--no-abbrev", "--format=",
              f"--since={since}", upstream)
    for line in raw.splitlines():
        if not line.startswith(":"):
            continue
        meta, path = line.split("\t", 1)
        f = meta.split()
        if f[4] != "A":
            seen[path].add(f[2])
        if f[4] != "D":
            seen[path].add(f[3])
    return seen


def diff_size(a, b):
    total = 0
    for line in git("diff", "--numstat", a, b).splitlines():
        added, removed, _ = line.split("\t", 2)
        if added.isdigit():
            total += int(added) + int(removed)
    return total


def write_blob(sha, path):
    data = git("cat-file", "blob", sha, binary=True) if sha != Z else b""
    with open(path, "wb") as fh:
        fh.write(data)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    upstream = sys.argv[1]
    base_rev = open(STATE_FILE).read().split()[0]
    base, ours, theirs = tree(base_rev), tree("HEAD"), tree(upstream)
    history = upstream_history(upstream, "2024-06-01")
    conflicts, local = [], []

    for path in sorted(set(base) | set(ours) | set(theirs)):
        o = ours.get(path, (None, Z))[1]
        u_mode, u = theirs.get(path, (None, Z))
        b = base.get(path, (None, Z))[1]
        if o == u or path in KEEP_DELETED or path in KEEP_OURS:
            continue
        if o == b or o in history.get(path, ()):
            # Unchanged by us, or an old copy of an upstream file.
            if u == Z:
                git("rm", "-q", "--cached", "--ignore-unmatch", path)
                if os.path.lexists(path):
                    os.remove(path)
            else:
                git("update-index", "--add", "--cacheinfo", f"{u_mode},{u},{path}")
                git("checkout", "--", path)
            continue
        if b == Z and u == Z:
            continue  # ours only
        if o == Z:
            print(f"deleted here, changed upstream (check by hand): {path}")
            continue
        local.append(path)
        cands = (history.get(path, set()) | {b}) - {Z}
        merge_base = min(cands, key=lambda c: diff_size(c, o)) if cands else Z
        with tempfile.TemporaryDirectory() as tmp:
            fo, fb, fu = (os.path.join(tmp, n) for n in ("o", "b", "u"))
            write_blob(o, fo)
            write_blob(merge_base, fb)
            write_blob(u, fu)
            res = subprocess.run(["git", "merge-file", "-p", "-L", "ours",
                                  "-L", "base", "-L", "upstream", fo, fb, fu],
                                 capture_output=True)
        with open(path, "wb") as fh:
            fh.write(res.stdout)
        git("add", path)
        if res.returncode:
            conflicts.append(path)

    print(f"local changes carried: {len(local)}")
    for path in local:
        print(f"  {path}")
    print(f"conflicts: {len(conflicts)}")
    for path in conflicts:
        print(f"  {path}")


if __name__ == "__main__":
    main()
