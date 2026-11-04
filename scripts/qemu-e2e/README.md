# QEMU first boot and OTA test

`e2e.py` boots an x86_64 OpenMANET image in QEMU and checks, from the host:

| Area | What is checked | Record |
|---|---|---|
| First boot | openmanetd serves the UI on `https://<node>:8081` and `http://<node>:8080`; `/setup` is served; `GetSetupStatus` reports the wizard enabled and setup not complete; RPCs refuse callers without a session; PAM sign-in as root works | D-038 |
| LuCI | LuCI answers on `:80`; with `frontend.luciProxy.enable: false` (the image default) `:8081/cgi-bin/luci/` is the OpenMANET UI; after turning it on, LuCI pages and `/luci-static/` load through `:8081` and `/api/system/info` reports the Advanced entry | D-037 |
| OTA | the image looks for updates in `ghostnet-labs/firmware`; the online check lists a newer release; the image is matched through `manifest.json`, downloaded, checked against `sha256sums` and handed to sysupgrade; the node reboots, keeps its settings, root password and openmanetd config, and loses non-config overlay files | D-039 |

The online update uses openmanetd's real code path. Inside the guest the test
points `api.github.com` at a local stand-in for the GitHub releases API
(`fakegithub.py`): an `/etc/hosts` entry, a throwaway CA added to the guest's
CA bundle, and an nftables DNAT from port 443 to the stand-in's port on the
host. The fake release is one major version ahead of the image and carries the
same image, so the test proves the flash by the overlay file that disappears.

QEMU has no HaLow radio, so the wizard cannot be applied here (openmanetd
refuses without a `morse` wifi-device); applying it stays a bench check on
the CM5 node (GHO-75).

## Run it

```sh
# needs qemu-system-x86_64, openssl and gzip; KVM if available
python3 scripts/qemu-e2e/e2e.py \
  --image bin/targets/x86/64/openmanet-*-x86-64-generic-squashfs-combined.img.gz \
  --workdir /tmp/qemu-e2e
python3 -m unittest discover -s scripts/qemu-e2e/tests -v
```

`--skip-ota` stops after the LuCI checks. The work directory gets
`console.log` (the whole serial console), `qemu.log`, `results.json` and
`summary.md`; on failure also `openmanetd-logread.txt`.

## CI

`build-pr-x86-64.yml` runs the job "QEMU first boot and OTA" after the x86-64
image build, on a GitHub-hosted runner (it needs KVM), and uploads the logs as
`qemu-e2e-logs`. "PR gate" waits for it like any other check.
