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

# QEMU mesh test (several nodes)

`mesh_e2e.py` boots several copies of the same x86_64 image (three by
default) and tests them as a mesh, the whole package end to end.

The nodes stand in a line. Node 1 and node 2 share one virtual radio link,
node 2 and node 3 the next. Each link is its own QEMU multicast segment
(`-netdev socket,mcast=...`, no root needed), so node 1 reaches node 3 only
through node 2. Each node is set up the way the setup wizard leaves a mesh
point: batman-adv BATMAN_V on `bat0` over the link NICs, bridged into
`br-ahwlan` at a bootstrap address `10.41.1.N/16`, firewall zone `ahwlan`
open. eth0 leaves `br-lan` for a test-only management interface at
`10.99.0.15/24` behind QEMU user networking, which is how the test reaches
each node's ports 80, 8080 and 8081 from the host. (openmanetd deletes the
`lan` section when it reserves an address, so the management path cannot
live there.)

Then the mesh joins the way a fresh one does in the field: openmanetd's
address reservation on each node picks a `10.41.x.y` (newer openmanetd
derives it from the node's MAC), configures DHCP and applies it in place;
older openmanetd reboots the node instead. QEMU runs with `-no-reboot`, so the
test powers a node back on whenever it reboots, and waits until every node has
reserved, is up on its reserved address, the addresses are unique and no node
has changed address or rebooted for 200 s. The reboot count is in the report
(0 means the address was applied in place). The checks after that use the reserved addresses. Every node also gets a
simulated GNSS receiver: the host writes a 1 Hz NMEA fix into the guest's
second serial port and gpsd reads it from `/dev/ttyS1`. The nodes stand about
400 m apart.

| Area | What is checked |
|---|---|
| Mesh join | every node reserves its own mesh address, the addresses are unique, and every node is up on its address (in place or after a reboot) |
| Mesh | batman-adv active on every link; node 1 has every node as an originator; node 1 reaches the last node through node 2 (multi-hop); ping both ways across `br-ahwlan` |
| openmanetd | up with sign-in on every node; `GetMeshTopology` on node 1 lists every node, only the real links, the right hop counts and every hostname; pages `/`, `/setup`, `/join`, `/coverage`, `/weather` |
| Across the mesh | the last node opens node 1's web app (`https://10.41.1.1:8081/`) and LuCI (`:80`); LuCI on `:80` on every node |
| ATAK | SA (`239.2.3.1:6969`) and GeoChat (`224.10.10.1:17012`) multicast sent on node 1 arrives at the last node; with `tak.enable` and the simulated fix, node 1's own CoT feed arrives there |
| Shared data | node 1's jam report (alfred gossip), weather station (simulated sensor, `weather.sensor.model: sim`) and coverage map show on the last node; an ATAK APK uploaded on node 1 downloads from the last node with the same SHA-256 |

A check whose feature is not in the image's openmanetd yet (found by its
config key in the binary) is reported as `skip`, so the test runs on older
images and starts checking a feature the day it ships. Copying the APK to
every node is not built yet; the test reports whether the last node has its
own copy as `info`.

To have hostnames now, the test runs the alfred bat-hosts facter on every
node instead of waiting for its 5-minute timer.

The guest has no HaLow or Wi-Fi radio, so radio behaviour (applying the
wizard, channel surveys, real jamming, signal levels) stays a bench check.

```sh
python3 scripts/qemu-e2e/mesh_e2e.py \
  --image bin/targets/x86/64/openmanet-*-x86-64-generic-squashfs-combined.img.gz \
  --nodes 3 --workdir /tmp/qemu-mesh
```

The work directory gets `results.json`, `summary.md`, and per node
`node*/console.log`, `qemu.log` and `logread.txt`; `node1/topology.json` is
node 1's last `GetMeshTopology` answer, and on a mesh failure
`node*/mesh-diag.txt` has addresses, bridges and batman-adv tables.
`--hold SECONDS` leaves the VMs running after the checks for debugging. Without KVM (`--accel tcg`) a run
takes about an hour; with KVM about 10 minutes.

## CI

- `build-pr-x86-64.yml` runs the job "QEMU mesh (3 nodes)" after the x86-64
  image build on every PR that rebuilds it, on a GitHub-hosted runner (KVM),
  and uploads `qemu-mesh-logs`. "PR gate" waits for it.
- `qemu-mesh-nightly.yml` runs the same test every night at 09:40 UTC on the
  newest x86-64 image built from 24.10 (the full build's artifact). A red
  run is the alert: ci-watch reports any failed scheduled run. Run it by hand
  with a `run_id` to test the image from a given run.
