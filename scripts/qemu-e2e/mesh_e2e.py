#!/usr/bin/env python3
"""Boot several OpenMANET x86_64 images in QEMU as a mesh and test it end to end.

Usage:
    mesh_e2e.py --image openmanet-...-x86-64-generic-squashfs-combined.img.gz
                [--nodes 3] [--workdir DIR] [--accel auto|kvm|tcg]

Each node is a full firmware image in its own VM. The nodes stand in a
line: node 1 and node 2 share one virtual radio link, node 2 and node 3
the next, and so on. Every link is its own QEMU multicast segment, so a
node hears only its neighbours and traffic from node 1 reaches node 3
only if node 2 routes it. The mesh is set up the way the setup wizard
leaves a mesh point (batman-adv BATMAN_V on bat0, bridged into br-ahwlan
at 10.41.1.N/16), with the virtio NICs standing in for the HaLow radios.
br-lan moves to a management address behind QEMU user networking, which
is how the test reaches each node's web ports. Each node also gets a
simulated GNSS receiver on its second serial port (gpsd on /dev/ttyS1),
standing ~400 m east of the one before.

Checks (a check whose feature is not in the image is reported as skip):
  mesh      batman-adv interfaces active, every node an originator,
            node 1 reaches the far end through the middle node, ping
            across br-ahwlan
  daemon    openmanetd up and sign-in works on every node; the mesh
            topology RPC shows every node, the right links, hop counts
            and hostnames; pages /, /setup, /join, /coverage, /weather
  across    one node's web app and LuCI reached from another node over
            the mesh; LuCI on :80 on every node
  ATAK      SA (239.2.3.1:6969) and GeoChat (224.10.10.1:17012)
            multicast sent on node 1 arrives at the far node; with
            tak.enable the node's own CoT feed arrives there too
  sharing   jam reports, weather (simulated sensor), coverage maps and
            the ATAK APK seen across the mesh

The guest has no HaLow or Wi-Fi radio, so radio-level behaviour (the
wizard itself, channel survey, jamming signals) stays a bench check.

Needs qemu-system-x86_64 and gzip on the host. Python stdlib only.
Writes per-node console logs, results.json and summary.md to the work
directory.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import e2e  # noqa: E402
import meshlib as ml  # noqa: E402
from console import Console, ConsoleError  # noqa: E402

TEST_PASSWORD = e2e.TEST_PASSWORD
BOOT_TIMEOUT = {"kvm": 300, "tcg": 1200}
CONVERGE_TIMEOUT = {"kvm": 1200, "tcg": 4800}
SA_GROUP, SA_PORT = "239.2.3.1", 6969
CHAT_GROUP, CHAT_PORT = "224.10.10.1", 17012
TOPO_RPC = "openmanet.mesh_topology.v1.MeshTopologyService/GetMeshTopology"
JAM_RPC = "openmanet.jamdetect.v1.JamDetectService/GetJamStatus"
WEATHER_RPC = "openmanet.weather.v1.WeatherService/ListWeatherStations"
COVERAGE_RPC = "openmanet.coverage.v1.CoverageService/GetCoverageMap"


# Each node's br-ahwlan address once openmanetd's address reservation settled.
IPS = {}


def ip(n):
    return IPS.get(n) or ml.node_ip(n)


class MeshVM(e2e.VM):
    """One firmware VM: management user net on eth0, one NIC per air link,
    console on ttyS0 and a simulated GNSS receiver on ttyS1."""

    def __init__(self, n, disk, ports, air_ports, accel, workdir):
        super().__init__(disk, ports, accel, workdir)
        self.n = n
        self.air_ports = air_ports
        self.gps_sock = os.path.join(workdir, "gps.sock")
        self._gps_stop = threading.Event()
        self._gps = None
        self.reboots = 0
        self.ready = None
        self.started_at = 0.0
        self.net_before = ""

    def cmd(self):
        fwd = ",".join("hostfwd=tcp:127.0.0.1:{}-{}:{}".format(h, ml.MGMT_GUEST_IP, g)
                       for g, h in self.ports.items())
        cmd = [
            "qemu-system-x86_64", "-machine", "q35", "-m", "1024",
            "-smp", "2" if self.accel == "kvm" else "1",
            "-accel", self.accel, "-display", "none", "-monitor", "none",
            "-no-user-config", "-no-reboot",
            "-drive", "file={},format=raw,if=virtio".format(self.disk),
            "-netdev", "user,id=lan,net={},host={},{}".format(ml.MGMT_NET, ml.MGMT_HOST_IP, fwd),
            "-device", "virtio-net-pci,netdev=lan,mac={}".format(ml.lan_mac(self.n)),
        ]
        for k, port in enumerate(self.air_ports, 1):
            cmd += ["-netdev", "socket,id=air{},mcast=230.0.0.1:{}".format(k, port),
                    "-device", "virtio-net-pci,netdev=air{},mac={}".format(k, ml.air_mac(self.n, k))]
        cmd += [
            "-chardev", "socket,id=con,path={},server=on,wait=off".format(self.sock),
            "-serial", "chardev:con",
            "-chardev", "socket,id=gps,path={},server=on,wait=off".format(self.gps_sock),
            "-serial", "chardev:gps",
        ]
        if self.accel == "kvm":
            cmd[1:1] = ["-cpu", "host"]
        return cmd

    def start(self):
        if os.path.exists(self.gps_sock):
            os.remove(self.gps_sock)
        super().start()
        self.started_at = time.monotonic()
        if self._gps is None:  # it reconnects on its own after a power cycle
            self._gps = threading.Thread(target=self._feed_gps, daemon=True)
            self._gps.start()

    def run(self, *a, **kw):
        # While the node reboots, wait for its new console: a line typed at
        # GRUB's menu would stop the boot.
        if self.ready is not None and not self.ready.wait(BOOT_TIMEOUT[self.accel]):
            raise ConsoleError("node{} did not come back after a reboot".format(self.n))
        return self.con.run(*a, **kw)

    def supervise(self, boot_timeout):
        """Power the node back on whenever it reboots (QEMU runs with -no-reboot),
        like a real node would come back. Counts the reboots."""
        self.reboots = 0
        self.ready = threading.Event()
        self.ready.set()
        self._sup = threading.Thread(target=self._supervise, args=(boot_timeout,), daemon=True)
        self._sup.start()

    def _supervise(self, boot_timeout):
        while not self._gps_stop.is_set():
            proc = self.proc
            try:
                proc.wait(1)
            except subprocess.TimeoutExpired:
                continue
            if self._gps_stop.is_set():
                return
            self.ready.clear()
            self.reboots += 1
            print("[node{}] rebooted by itself ({}); powering it back on".format(self.n, self.reboots),
                  flush=True)
            self.con.close()
            self.start()
            try:
                self.login(boot_timeout)
            except ConsoleError as ex:
                print("[node{}] no console after reboot: {}".format(self.n, ex), flush=True)
                continue
            self.ready.set()

    def _feed_gps(self):
        """Write a 1 Hz NMEA fix into the guest's ttyS1."""
        lat, lon = ml.node_position(self.n)
        sock = None
        while not self._gps_stop.is_set():
            try:
                if sock is None:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(self.gps_sock)
                now = datetime.datetime.now(datetime.timezone.utc)
                sock.sendall(ml.nmea_fix(lat, lon, now).encode())
            except OSError:
                if sock:
                    sock.close()
                sock = None
            self._gps_stop.wait(1)
        if sock:
            sock.close()

    def stop(self):
        self._gps_stop.set()
        super().stop()


def push_text(vm, path, text):
    """Write a text file in the guest through the console, a few lines a command."""
    vm.run(": > " + path)
    chunk = []
    size = 0
    for line in text.splitlines():
        q = ml.shell_quote(line)
        if chunk and size + len(q) > 300:
            vm.run("printf '%s\\n' {} >> {}".format(" ".join(chunk), path))
            chunk, size = [], 0
        chunk.append(q)
        size += len(q) + 1
    if chunk:
        vm.run("printf '%s\\n' {} >> {}".format(" ".join(chunk), path))


def wait_for(fn, timeout, interval=3):
    """Poll fn() until it returns a truthy value or the time runs out."""
    deadline = time.monotonic() + timeout
    last = None
    while True:
        try:
            last = fn()
        except (OSError, urllib.error.URLError, ConsoleError) as e:
            last = None
            err = e
        else:
            err = None
        if last:
            return last
        if time.monotonic() > deadline:
            return last if err is None else None
        time.sleep(interval)


# openmanetd config keys that show a feature is in the image's binary.
FEATURE_MARKERS = {
    "tak": "tak.interval",
    "coverage": "tak.coverage.enable",
    "weather": "weather.sensor.model",
    "jamdetect": "jamdetect.gossipInterval",
    "onboard": "onboard.maxApkBytes",
}


def image_features(vm):
    """Which newer openmanetd features this image has, from one pass over the binary."""
    args = " ".join("-e " + ml.shell_quote(m) for m in FEATURE_MARKERS.values())
    _, out = vm.run("grep -a -o -F {} \"$(command -v openmanetd)\" | sort -u".format(args),
                    timeout=300, check=False)
    found = set(out.split())
    return {name for name, marker in FEATURE_MARKERS.items() if marker in found}


def mesh_uci(n, ifaces):
    """uci commands that turn a fresh image into mesh node n."""
    c = [
        "set system.@system[0].hostname=node{}".format(n),
        # Management access for the test on eth0, outside br-lan: openmanetd
        # deletes the 'lan' section when it reserves the node's address.
        "set network.e2emgmt=interface",
        "set network.e2emgmt.proto=static",
        "set network.e2emgmt.device=eth0",
        "set network.e2emgmt.ipaddr=" + ml.MGMT_GUEST_IP,
        "set network.e2emgmt.netmask=255.255.255.0",
        "set firewall.e2emgmt=zone",
        "set firewall.e2emgmt.name=e2emgmt",
        "add_list firewall.e2emgmt.network=e2emgmt",
        "set firewall.e2emgmt.input=ACCEPT",
        "set firewall.e2emgmt.output=ACCEPT",
        "set firewall.e2emgmt.forward=REJECT",
        "delete network.wan", "delete network.wan6",
        "set network.bat0=interface", "set network.bat0.proto=batadv",
        "set network.bat0.routing_algo=BATMAN_V",
        "set network.bat0.multicast_mode=0",
        "set network.bat0.bridge_loop_avoidance=1",
        "set network.bat0.hop_penalty=30",
        "set network.bat0.bonding=1",
        "set network.bat0.aggregated_ogms=1",
        "set network.bat0.ap_isolation=0",
        "set network.bat0.fragmentation=1",
        "set network.bat0.orig_interval=1000",
        "set network.bat0.distributed_arp_table=1",
        "set network.bat0.network_coding=1",
        "set network.bat0.gw_mode=client",
        "set network.ahwlan_dev=device",
        "set network.ahwlan_dev.name=br-ahwlan",
        "set network.ahwlan_dev.type=bridge",
        "add_list network.ahwlan_dev.ports=bat0",
        "set network.ahwlan=interface",
        "set network.ahwlan.proto=static",
        "set network.ahwlan.device=br-ahwlan",
        "set network.ahwlan.ipaddr=" + ml.node_ip(n),
        "set network.ahwlan.netmask=255.255.0.0",
        "set firewall.ahwlan=zone",
        "set firewall.ahwlan.name=ahwlan",
        "add_list firewall.ahwlan.network=ahwlan",
        "set firewall.ahwlan.input=ACCEPT",
        "set firewall.ahwlan.output=ACCEPT",
        "set firewall.ahwlan.forward=ACCEPT",
        "set gpsd.core.device=/dev/ttyS1",
        "set gpsd.core.enabled=1",
    ]
    for k, dev in enumerate(ifaces):
        c += [
            "set network.air{}_dev=device".format(k),
            "set network.air{}_dev.name={}".format(k, dev),
            "set network.air{}_dev.mtu=1560".format(k),
            "set network.batmesh{}=interface".format(k),
            "set network.batmesh{}.proto=batadv_hardif".format(k),
            "set network.batmesh{}.master=bat0".format(k),
            "set network.batmesh{}.device={}".format(k, dev),
        ]
    return c


def openmanetd_overrides(n, features):
    """Config blocks for node n: the ATAK feed and its coverage map on every
    node, a simulated weather sensor on node 1."""
    blocks = {}
    if "tak" in features:
        tak = {"enable": True, "interval": "5s", "jamming": True}
        if "coverage" in features:
            tak["coverage"] = {"enable": True}
        blocks["tak"] = tak
    if "weather" in features and n == 1:
        blocks["weather"] = {"enable": True, "publishInterval": "10s",
                             "sensor": {"enable": True, "model": "sim"}}
    return blocks


def set_up_node(vm, boot_timeout, features_out):
    """Boot one node and make it a mesh point. Runs in its own thread."""
    vm.start()
    vm.login(boot_timeout)
    # The image writes /etc/config/network about a minute into first boot.
    ok = wait_for(lambda: vm.run("grep -q br-lan /etc/config/network", check=False)[0] == 0, 300)
    if not ok:
        raise ConsoleError("node{}: /etc/config/network never appeared".format(vm.n))
    # First boot writes the network config in several steps; let it finish.
    wait_for(lambda: int(vm.run("echo $(( $(date +%s) - $(date -r /etc/config/network +%s) ))",
                                check=False)[1].strip() or 0) > 30, 300, interval=10)
    # openmanetd rewrites /etc/config/network itself (address reservation);
    # keep it from racing the edits below.
    vm.run("/etc/init.d/openmanetd stop", timeout=60, check=False)
    vm.run("printf '%s\\n%s\\n' '{0}' '{0}' | passwd root >/dev/null 2>&1".format(TEST_PASSWORD))
    _, out = vm.run("for d in /sys/class/net/*; do echo \"${d##*/} $(cat $d/address)\"; done")
    macs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            macs[parts[1].lower()] = parts[0]
    ifaces = [macs[ml.air_mac(vm.n, k)] for k in range(1, len(vm.air_ports) + 1)]
    # Take eth0 out of br-lan so the management interface can have it.
    vm.run("for s in $(uci show network | sed -n \"s/^network\\.\\([^.]*\\)\\.name='br-lan'$/\\1/p\"); "
           "do uci -q del_list network.$s.ports=eth0; done; true")
    cmds = mesh_uci(vm.n, ifaces)
    missing = cmds
    for _ in range(3):
        for i in range(0, len(missing), 6):
            vm.run(" ; ".join("uci -q " + c for c in missing[i:i + 6]) + " ; true")
        vm.run("uci commit")
        _, shown = vm.run("uci show network; uci show firewall; uci show gpsd; uci show system",
                          timeout=60, check=False)
        missing = ml.missing_uci(cmds, shown)
        if not missing:
            break
    if missing:
        raise ConsoleError("node{}: uci settings did not stick: {}".format(vm.n, missing))

    features = image_features(vm)
    features_out[vm.n] = features
    blocks = openmanetd_overrides(vm.n, features)
    if blocks:
        _, cfg = vm.run("cat /etc/openmanetd/config.yml")
        for key in blocks:
            cfg = ml.drop_yaml_block(cfg, key)
        cfg = cfg.rstrip("\n") + "\n" + "".join(ml.yaml_block(k, v) for k, v in blocks.items())
        push_text(vm, "/etc/openmanetd/config.yml", cfg)

    vm.run("/etc/init.d/system reload", timeout=60)
    vm.run("/etc/init.d/network restart", timeout=120)
    vm.run("/etc/init.d/firewall restart", timeout=120, check=False)
    vm.run("/etc/init.d/gpsd restart", timeout=60, check=False)
    _, vm.net_before = vm.run("uci show network", timeout=60)
    vm.run("/etc/init.d/openmanetd start", timeout=60)
    wait_for(lambda: vm.run("ip -4 addr show br-ahwlan | grep -q {}".format(ml.node_ip(vm.n)),
                            check=False)[0] == 0, 120)


# openmanetd's address reservation runs every 125 s; a node that has kept
# its address for longer than this has settled.
SETTLED_FOR = 200


def node_state(vm):
    """(reserved, uci address, address live on br-ahwlan) as the node
    reports it, or None if unreachable."""
    if not vm.ready.is_set():
        return None
    try:
        _, out = vm.run("c=$(uci -q get openmanetd.config.dhcpconfigured); echo ${c:-0}; "
                        "a=$(uci -q get network.ahwlan.ipaddr); echo ${a:-none}; ip -4 addr show br-ahwlan | grep -q \"inet $a/\" && echo live || echo pending",
                        timeout=60, check=False)
    except (ConsoleError, OSError):
        return None
    lines = out.split()
    if len(lines) < 3:
        return None
    return lines[0] == "1", lines[1], lines[2] == "live"


def converge(res, vms, timeout):
    """Let openmanetd's address reservation run as it does on a fresh mesh:
    each node picks an address (newer openmanetd: from its MAC) and moves off
    the bootstrap address, in place or (older openmanetd) by rebooting; a
    node whose pick collides with a lower-MAC peer picks again. Wait until
    every node has reserved, uses the reserved address, the addresses are
    unique and no node has changed address or rebooted for SETTLED_FOR
    seconds."""
    deadline = time.monotonic() + timeout
    states = {}
    last = {}
    changed = {vm.n: time.monotonic() for vm in vms}
    while time.monotonic() < deadline:
        states = {vm.n: node_state(vm) for vm in vms}
        now = time.monotonic()
        for n, st in states.items():
            if st and st != last.get(n):
                last[n] = st
                changed[n] = now
        quiet = all(now - max(vm.started_at, changed[vm.n]) > SETTLED_FOR for vm in vms)
        done = all(st and st[0] and st[2] for st in states.values())
        addrs = [st[1] for st in states.values() if st]
        if done and quiet and len(set(addrs)) == len(vms):
            break
        time.sleep(15)
    reboots = {vm.n: vm.reboots for vm in vms}
    res.note("reboots during address reservation (0 = applied in place)", ", ".join(
        "node{} {}".format(n, r) for n, r in sorted(reboots.items())))
    ok = all(st and st[0] for st in states.values())
    res.check("every node reserved its mesh address (openmanetd address reservation)", ok,
              states, fatal=True)
    addrs = {n: st[1] for n, st in states.items()}
    res.check("mesh addresses are unique", len(set(addrs.values())) == len(vms), addrs, fatal=True)
    res.check("every node is up on its reserved address",
              all(st and st[2] for st in states.values()),
              ", ".join("node{} {}".format(n, "live" if st and st[2] else "not applied")
                        for n, st in sorted(states.items())))
    IPS.update(addrs)
    for vm in vms:
        _, after = vm.run("uci show network", timeout=60, check=False)
        lost = ml.lost_uci_lines(vm.net_before, after)
        with open(os.path.join(vm.workdir, "network-uci.txt"), "w") as f:
            f.write("## before address reservation\n{}\n## after\n{}\n".format(vm.net_before, after))
        res.check("node{}: mesh network config intact after address reservation".format(vm.n),
                  not lost, "lost: " + "; ".join(lost) if lost else "")
    for vm in vms:
        wait_for(lambda vm=vm: vm.run("ip -4 addr show br-ahwlan | grep -q 'inet {}/'".format(ip(vm.n)),
                                      check=False)[0] == 0, 120)


def guest_fetch(vm, url, timeout=60):
    """Fetch a URL from inside the guest; returns (exit status, body)."""
    # No -q: uclient-fetch then prints "HTTP error 403" and the like.
    return vm.run("wget -T 15 --no-check-certificate -O - {} 2>&1".format(url),
                  timeout=timeout, check=False)


def capture_start(vm, name, filt):
    # The image has no timeout(1); capture_stop ends every capture.
    vm.run("rm -f /tmp/{0}.cap; (tcpdump -n -l -A -i br-ahwlan {1} "
           "> /tmp/{0}.cap 2>&1 &)".format(name, ml.shell_quote(filt)))


def capture_stop(vm):
    vm.run("killall tcpdump 2>/dev/null; true", check=False)


def capture_has(vm, name, needle):
    code, _ = vm.run("grep -q -F {} /tmp/{}.cap".format(ml.shell_quote(needle), name), check=False)
    return code == 0


def test_mesh(res, vms, pairs):
    count = len(vms)
    first, last = vms[0], vms[-1]
    for vm in vms:
        _, out = vm.run("batctl if", check=False)
        active = out.count("active")
        res.check("node{}: batman-adv on every air link".format(vm.n),
                  active == len(vm.air_ports) and "inactive" not in out, out.replace("\n", "; "))

    # -n (an `o` option): print MACs, not /etc/bat-hosts names. Nodes fill bat-hosts from
    # alfred, so once it has entries plain `batctl o` shows "node2_eth1".
    def originators():
        _, out = first.run("batctl o -n -H", check=False)
        best = ml.parse_originators(out)
        seen = {ml.node_of_mac(o, count) for o in best}
        return (best, out) if all(n in seen for n in range(2, count + 1)) else None

    got = wait_for(originators, 120)
    best, raw = got if got else ({}, first.run("batctl o -n -H", check=False)[1])
    res.check("node1 sees every other node as a batman-adv originator", bool(got),
              raw.replace("\n", "; "))
    if count >= 3:
        far = [o for o in best if ml.node_of_mac(o, count) == count]
        hop = ml.node_of_mac(best[far[0]], count) if far else None
        res.check("multi-hop: node1 reaches node{} through node2".format(count), hop == 2,
                  "next hop node{}".format(hop) if hop else raw.replace("\n", "; "))
    for vm in vms[1:]:
        code, out = first.run("ping -c 3 -W 3 {}".format(ip(vm.n)), timeout=60, check=False)
        res.check("ping node1 -> node{} across the mesh ({})".format(vm.n, ip(vm.n)),
                  code == 0, out.splitlines()[-1] if out else "")
    code, out = last.run("ping -c 3 -W 3 {}".format(ip(1)), timeout=60, check=False)
    res.check("ping node{} -> node1 across the mesh".format(count), code == 0,
              out.splitlines()[-1] if out else "")
    if res.failed():
        for vm in (first, last):
            _, diag = vm.run("ip -br addr; bridge link; ip neigh; batctl n; batctl tg; "
                             "uci show network | grep -E 'ahwlan|bat0|batmesh'", timeout=60, check=False)
            with open(os.path.join(vm.workdir, "mesh-diag.txt"), "w") as f:
                f.write(diag)


def page_in_bundle(node, route):
    """True when the SPA bundle routes `route` (lazy pages still name their route there)."""
    code, html = node.ui("/")
    if code != 200:
        return False
    for src in re.findall(r'src="(/assets/[^"]+\.js)"', html):
        c, js = node.ui(src)
        if c == 200 and '"{}"'.format(route) in js:
            return True
    return False


def test_daemon(res, vms, nodes, pairs):
    count = len(vms)
    for vm, node in zip(vms, nodes):
        try:
            node.wait_ready(300)
            ok = True
        except e2e.CheckFailed as ex:
            ok = False
            res.check("node{}: openmanetd answers".format(vm.n), False, ex)
            continue
        code, _ = node.login(TEST_PASSWORD)
        res.check("node{}: openmanetd up and sign-in works".format(vm.n), ok and code == 200,
                  "HTTP {}".format(code))

    node = nodes[0]
    for route in ("/", "/setup", "/join", "/coverage", "/weather"):
        if route not in ("/", "/setup") and not page_in_bundle(node, route):
            res.skip("page {} served".format(route), "page not in this image's web app")
            continue
        code, body = node.ui(route)
        res.check("page {} served".format(route), code == 200 and 'id="root"' in body,
                  "HTTP {}".format(code))

    # Speed up the 5-minute alfred facters (bat-hosts) so hostnames gossip now.
    for _ in range(2):
        for vm in vms:
            vm.run("lua /etc/alfred/bat-hosts.lua", timeout=60, check=False)
        time.sleep(12)

    def topo_ok():
        code, body = node.rpc(TOPO_RPC)
        if code != 200:
            return None
        found = ml.check_topology(body.get("topology", body), count, pairs)
        return (found, body) if all(ok for ok, _ in found.values()) else None

    got = wait_for(topo_ok, 180, interval=10)
    if got:
        found, body = got
    else:
        code, body = node.rpc(TOPO_RPC)
        found = ml.check_topology(body.get("topology", body), count, pairs) if code == 200 else {}
    with open(os.path.join(vms[0].workdir, "topology.json"), "w") as f:
        json.dump(body, f, indent=2)
    labels = {"nodes": "topology lists every node", "edges": "topology links match the radio links",
              "hops": "topology hop counts match the line", "hostnames": "topology shows node hostnames"}
    for key, label in labels.items():
        ok, detail = found.get(key, (False, "GetMeshTopology failed: {}".format(str(body)[:200])))
        res.check(label, ok, detail)


def test_across(res, vms, nodes):
    last = vms[-1]
    code, out = guest_fetch(last, "https://{}:8081/".format(ip(1)))
    res.check("node{} opens node1's web app over the mesh (https :8081)".format(last.n),
              code == 0 and 'id="root"' in out, out[:200])
    code, out = guest_fetch(last, "http://{}/cgi-bin/luci/".format(ip(1)))
    body = "\n".join(l for l in out.splitlines()
                     if not l.startswith(("Downloading", "Connecting", "Writing")))
    res.check("node{} reaches node1's LuCI over the mesh (:80)".format(last.n),
              e2e.looks_like_luci(body) or "HTTP error 403" in body, out[:200])
    for vm, node in zip(vms, nodes):
        code, body = node.request(node.url("http", 80, "/cgi-bin/luci/"))
        res.check("node{}: LuCI on :80".format(vm.n),
                  code in (200, 403) and e2e.looks_like_luci(body), "HTTP {}".format(code))


def test_atak(res, vms, nodes, features):
    first, last = vms[0], vms[-1]
    token = "E2E{}".format(int(time.time()))
    capture_start(last, "sa", "udp and dst port {} and src host {}".format(SA_PORT, ip(1)))
    capture_start(last, "chat", "udp and dst port {} and src host {}".format(CHAT_PORT, ip(1)))
    time.sleep(3)
    first.run("ip route replace 224.0.0.0/4 dev br-ahwlan")
    for _ in range(3):
        first.run(ml.lua_mcast_send(SA_GROUP, SA_PORT, "SA-" + token), timeout=30, check=False)
        first.run(ml.lua_mcast_send(CHAT_GROUP, CHAT_PORT, "CHAT-" + token), timeout=30, check=False)
        time.sleep(1)
    sa = wait_for(lambda: capture_has(last, "sa", "SA-" + token), 20)
    res.check("ATAK SA multicast ({}:{}) from node1 arrives at node{}".format(SA_GROUP, SA_PORT, last.n),
              sa, "")
    chat = wait_for(lambda: capture_has(last, "chat", "CHAT-" + token), 20)
    res.check("ATAK GeoChat multicast ({}:{}) from node1 arrives at node{}".format(
        CHAT_GROUP, CHAT_PORT, last.n), chat, "")
    capture_stop(last)

    if "tak" not in features:
        res.skip("node1's own ATAK CoT feed arrives at node{}".format(last.n),
                 "ATAK feed (tak.*) not in this image's openmanetd")
        return
    # The feed needs openmanetd's GNSS service, which it starts only on
    # boards it knows (board.GNSSsupoorted); the x86 image has none, so the
    # status answers without a position.
    _, gnss = nodes[0].rpc("openmanet.gnss.v1.GNSSService/GetGNSSStatus")
    if "position" not in gnss:
        res.skip("node1's own ATAK CoT feed arrives at node{}".format(last.n),
                 "openmanetd runs no GNSS service on this board (x86), so there is no fix to send")
        return
    res.check("node1 has a GNSS fix from the simulated receiver",
              gnss["position"].get("fixType", "") not in ("", "FIX_TYPE_UNSPECIFIED", "FIX_TYPE_NO_FIX"),
              gnss["position"])
    capture_start(last, "cot", "udp and dst host {} and dst port {} and src host {}".format(
        SA_GROUP, SA_PORT, ip(1)))
    got = wait_for(lambda: last.run("grep -c 'UDP' /tmp/cot.cap", check=False)[1].strip() not in ("", "0"),
                   60, interval=5)
    capture_stop(last)
    _, cap = last.run("tail -n 5 /tmp/cot.cap", check=False)
    res.check("node1's own ATAK CoT feed arrives at node{} (tak.enable, GNSS fix)".format(last.n),
              got, cap.replace("\n", " ")[:300])


def test_sharing(res, vms, nodes, features, workdir):
    last_vm, far = vms[-1], nodes[-1]

    if "jamdetect" in features:
        def jam():
            code, body = far.rpc(JAM_RPC)
            return body if code == 200 and ml.mentions_node(body.get("remote", []), 1) else None
        body = wait_for(jam, 90, interval=10)
        if not body:
            body = far.rpc(JAM_RPC)[1]
        res.check("node{} has node1's jam report (alfred gossip)".format(last_vm.n), bool(body)
                  and ml.mentions_node(body.get("remote", []), 1), json.dumps(body)[:300])
    else:
        res.skip("jam reports shared across the mesh", "jam detection not in this image")

    if "weather" in features:
        def wx():
            code, body = far.rpc(WEATHER_RPC)
            return body if code == 200 and ml.mentions_node(body, 1) else None
        body = wait_for(wx, 120, interval=10)
        if not body:
            body = far.rpc(WEATHER_RPC)[1]
        res.check("node{} lists node1's weather station (simulated sensor)".format(last_vm.n),
                  bool(body) and ml.mentions_node(body, 1), json.dumps(body)[:300])
    else:
        res.skip("weather station shared across the mesh", "weather stations not in this image")

    if "coverage" in features:
        def cov():
            code, body = far.rpc(COVERAGE_RPC)
            return body if code == 200 and ml.mentions_node(body.get("remote", []), 1) else None
        body = wait_for(cov, 180, interval=10)
        if not body:
            body = far.rpc(COVERAGE_RPC)[1]
        res.check("node{} has node1's coverage map".format(last_vm.n),
                  bool(body) and ml.mentions_node(body.get("remote", []), 1), json.dumps(body)[:300])
    else:
        res.skip("coverage map shared across the mesh", "coverage map not in this image")

    if "onboard" not in features:
        res.skip("ATAK APK served across the mesh", "phone onboarding not in this image")
        return
    apk = ml.fake_apk()
    digest = hashlib.sha256(apk).hexdigest()
    body, ctype = ml.multipart("file", "atak-e2e.apk", apk)
    code, text = nodes[0].request(nodes[0].url("https", 8081, "/api/onboard/atak"), data=body,
                                  headers={"Content-Type": ctype,
                                           "Authorization": "Bearer " + (nodes[0].token or "")})
    res.check("APK upload on node1", code in (200, 201), "HTTP {} {}".format(code, text[:200]))
    code, out = last_vm.run("wget -q -T 20 -O /tmp/atak.apk http://{}:8080/onboard/atak.apk && "
                            "sha256sum /tmp/atak.apk".format(ip(1)), timeout=90, check=False)
    res.check("node{} downloads node1's APK over the mesh (sha256 matches)".format(last_vm.n),
              code == 0 and digest in out, out[:200])
    code, st = far.request(far.url("http", 8080, "/onboard/status"))
    copied = code == 200 and '"available":true' in st.replace(" ", "")
    if copied:
        res.check("APK copied to node{} by the mesh".format(last_vm.n), True)
    else:
        res.note("APK copied to node{} by the mesh".format(last_vm.n),
                 "no: copying the APK to every node is not built yet")


def collect_logs(vms):
    for vm in vms:
        if not vm.con:
            continue
        try:
            _, logs = vm.run("logread | tail -n 300", timeout=30, check=False)
            with open(os.path.join(vm.workdir, "logread.txt"), "w") as f:
                f.write(logs)
        except (ConsoleError, OSError):
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--image", required=True, help="x86_64 squashfs combined .img.gz")
    ap.add_argument("--nodes", type=int, default=3, help="nodes in the line (default 3)")
    ap.add_argument("--workdir", help="where logs and disks go (default: a temp dir)")
    ap.add_argument("--accel", default="auto", choices=["auto", "kvm", "tcg"])
    ap.add_argument("--boot-timeout", type=int, default=0,
                    help="seconds to wait for each console (default 300 kvm, 1200 tcg)")
    ap.add_argument("--hold", type=int, default=0,
                    help="leave the VMs running this many seconds after the checks, for debugging")
    args = ap.parse_args(argv)
    if args.nodes < 2:
        ap.error("--nodes must be at least 2")

    workdir = args.workdir or tempfile.mkdtemp(prefix="qemu-mesh-e2e-")
    os.makedirs(workdir, exist_ok=True)
    accel = e2e.pick_accel(args.accel)
    boot_timeout = args.boot_timeout or BOOT_TIMEOUT[accel]
    print("work dir: {}  accel: {}  nodes: {}".format(workdir, accel, args.nodes), flush=True)

    base = os.path.join(workdir, "base.img")
    e2e.gunzip(os.path.abspath(args.image), base)
    air, pairs = ml.line_links(args.nodes, 41000 + (os.getpid() % 200) * 10)
    vms, nodes = [], []
    for n in range(1, args.nodes + 1):
        d = os.path.join(workdir, "node{}".format(n))
        os.makedirs(d, exist_ok=True)
        disk = os.path.join(d, "disk.img")
        shutil.copyfile(base, disk)
        ports = {80: e2e.free_port(), 8080: e2e.free_port(), 8081: e2e.free_port()}
        vms.append(MeshVM(n, disk, ports, air[n], accel, d))
        nodes.append(e2e.Node(ports))
    os.remove(base)

    res = e2e.Results("QEMU mesh test ({} nodes)".format(args.nodes))
    error = None
    try:
        features_by_node = {}
        errors = []

        def boot(vm):
            try:
                set_up_node(vm, boot_timeout, features_by_node)
            except Exception as ex:  # noqa: BLE001 - reported below
                errors.append("node{}: {}: {}".format(vm.n, type(ex).__name__, ex))

        threads = [threading.Thread(target=boot, args=(vm,)) for vm in vms]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        res.check("every node booted and joined the mesh config", not errors, "; ".join(errors),
                  fatal=True)
        for vm in vms:
            vm.supervise(boot_timeout)
        converge(res, vms, CONVERGE_TIMEOUT[accel])
        _, rel = vms[0].run("grep DISTRIB_DESCRIPTION /etc/openwrt_release")
        res.note("image", rel.split("=", 1)[-1].strip("'\""))
        _, ver = vms[0].run("opkg list-installed openmanetd 2>/dev/null || apk list -I openmanetd 2>/dev/null",
                            check=False)
        res.note("openmanetd", ver.strip() or "unknown")
        features = features_by_node.get(1, set())
        res.note("openmanetd features found", ", ".join(sorted(features)) or "none of the newer ones")

        test_mesh(res, vms, pairs)
        test_daemon(res, vms, nodes, pairs)
        test_across(res, vms, nodes)
        test_atak(res, vms, nodes, features)
        test_sharing(res, vms, nodes, features, workdir)
        if res.failed():
            error = "failed checks: " + "; ".join(res.failed())
            print("ERROR: " + error, file=sys.stderr, flush=True)
    except Exception as ex:  # noqa: BLE001 - any failure must still write the results
        if isinstance(ex, KeyboardInterrupt):
            raise
        error = "{}: {}".format(type(ex).__name__, ex)
        # QEMU runs with -no-reboot, so a node that rebooted on its own is gone.
        gone = [vm.n for vm in vms if vm.proc and vm.proc.poll() is not None]
        if gone:
            error += " (node {} rebooted or powered off by itself; see node*/console.log)".format(
                ", ".join(str(n) for n in gone))
        print("ERROR: " + error, file=sys.stderr, flush=True)
    finally:
        if args.hold:
            print("holding the VMs for {}s; consoles: {}".format(
                args.hold, " ".join(vm.sock for vm in vms)), flush=True)
            for vm in vms:
                if vm.con:
                    vm.con.close()
                    vm.con = None
            time.sleep(args.hold)
            for vm in vms:
                if vm.proc and vm.proc.poll() is None:
                    vm.con = Console(vm.sock, os.path.join(vm.workdir, "console.log"))
        collect_logs(vms)
        res.write(workdir, error)
        for vm in vms:
            vm.stop()
            try:
                os.remove(vm.disk)
            except OSError:
                pass
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
