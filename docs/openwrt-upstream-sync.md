# Syncing with OpenWrt 24.10

This repository is OpenWrt 24.10 plus the OpenMANET and ghostnet-labs changes.
It does not share git history with
[openwrt/openwrt](https://github.com/openwrt/openwrt): the tree was
squash-imported in 2025-12 ("initial commit", from OpenWrt v24.10.4), then
partly moved back to 24.10.2 and partly forward again by later releases. So
`git merge` from upstream does not work. Each sync is a file-by-file import
instead, done with `scripts/openwrt-upstream-sync.py`.

## Where we are

[`.openwrt-upstream`](../.openwrt-upstream) records the upstream commit the
tree was last synced to. It is the merge base for the next sync.

| Import | Upstream commit | Revision | Kernel | mac80211 |
| -- | -- | -- | -- | -- |
| 2026-11-03 | `34fa4b1af5` (openwrt-24.10 head, 2026-10-29) | r29306-34fa4b1af5 | 6.6.159 | 6.12.96 |

The `version` and `version.date` files hold that revision and its commit time,
because this repository has no OpenWrt history for `scripts/getver.sh` to
count. `include/version.mk` defaults to the newest point release before the
synced commit (24.10.8), so on-device package feeds point at a release that
does not get rebuilt.

## How to sync

1. Branch from `origin/24.10` and fetch upstream:

   ```sh
   git fetch --no-tags https://github.com/openwrt/openwrt.git \
       openwrt-24.10:refs/remotes/upstream/openwrt-24.10
   python3 scripts/openwrt-upstream-sync.py upstream/openwrt-24.10
   ```

   For each path, the script takes the new upstream file when ours is
   identical to any upstream version of it (an untouched file or an old
   backport), keeps files only we have, keeps the upstream files we removed on
   purpose (`KEEP_DELETED`), and 3-way merges our real local changes. It prints
   the local changes it carried and the files left with conflict markers.

2. Resolve the conflicts. Rules of thumb from the 2026-11 import:
   - A kernel patch we only refreshed to make it apply to an older kernel
     (for example the 6.6.138 repair in #41) takes the upstream version.
   - A kernel config line that both sides added keeps one copy.
   - Package sources we point at a vendor fork (Morse `iwinfo`) keep ours.

3. Update `.openwrt-upstream`, `version` (`r<count>-<10-char hash>`, from
   `scripts/getver.sh` in an upstream checkout) and `version.date`
   (`git log -1 --format=%ct <commit>`).

4. Bump the `packages`, `routing` and `telephony` pins in `feeds.conf.default`
   to the heads of their `openwrt-24.10` branches in the same PR.

5. Check that our patches still apply before spending a full build on it.
   Each check applies the patch directories in the order the build uses, with
   `patch -f -p1`:
   - Kernel: a `linux-6.6.<sublevel>` tree (a shallow clone of the tag from
     `github.com/gregkh/linux` works), with `target/linux/generic/files*`
     copied in, then `generic/backport-6.6`, `pending-6.6`, `hack-6.6`, then
     each target's `files*` and `patches-6.6`. Do every target, not just ours:
     kernel CI builds all affected targets.
   - `package/kernel/mac80211`: the backports tarball, then `patches/build`,
     `subsys`, `ath*`, `rt2x00`, `mt7601u`, `mwl`, `brcm`, `rtl` (the order
     in its Makefile). Our Morse S1G patches live in `subsys/9xx-*`.
   - `hostapd` (source from `github.com/jmalinen/hostap` at the pinned
     commit, with `src/` copied in first), `mt76`, `netifd`, `uhttpd`.
   - The board patches in `patches/<board>/` against the new
     `openwrt/packages` pin (`scripts/openmanet_setup.sh` applies them to
     `feeds/packages` and stops on the first failure).

   A patch that no longer applies because upstream already contains it gets
   deleted, with the upstream commit or version named in the PR.

## 2026-11-03 import notes

- Base before: v24.10.4 plus partial backports (kernel 6.6.138, mac80211
  6.12.61, openssl 3.0.19, dnsmasq 2.90). 253 files that were older copies of
  upstream files now match upstream head (this includes 28 upstream rockchip, armsr, octeon,
  qualcommax and realtek files dropped by the 2025-12 move back to 24.10.2, now
  restored), and 6 kernel patches that only carried the 6.6.138 repair take
  upstream's 6.6.159 versions.
- 68 files with real local changes were carried over. Three needed a hand
  merge: `package/network/utils/iwinfo/Makefile` (keep the Morse source),
  `target/linux/x86/config-6.6` (MITIGATION_TSA and MITIGATION_VMSCAPE were
  added on both sides) and `target/linux/lantiq/patches-6.6/0101-find_active_root.patch`
  (upstream version).
- Dropped `package/kernel/mac80211/patches/subsys/904-0001-wifi-mac80211-don-t-perform-DA-check-on-S1G-beacon.patch`:
  backports 6.12.96 already has the fix.
- Dropped `patches/*/0003-iperf3_3_19_1.patch`: the new `openwrt/packages` pin
  ships iperf3 3.22.
- Patch checks: kernel patches apply on all 36 targets, mac80211 (224 patches),
  hostapd, mt76, netifd, uhttpd and every board patch apply cleanly.
