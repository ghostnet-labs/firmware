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
SA_GROUP, SA_PORT = "239.2.3.1", 6969
CHAT_GROUP, CHAT_PORT = "224.10.10.1", 17012
TOPO_RPC = "openmanet.mesh_topology.v1.MeshTopologyService/GetMeshTopology"
JAM_RPC = "openmanet.jamdetect.v1.JamDetectService/GetJamStatus"
WEATHER_RPC = "openmanet.weather.v1.WeatherService/ListWeatherStations"
COVERAGE_RPC = "openmanet.coverage.v1.CoverageService/GetCoverageMap"


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
        self._gps = threading.Thread(target=self._feed_gps, daemon=True)
        self._gps.start()

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
        "set network.lan.ipaddr=" + ml.MGMT_GUEST_IP,
        "set network.lan.netmask=255.255.255.0",
        "delete network.lan.ip6assign",
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
    vm.run("printf '%s\\n%s\\n' '{0}' '{0}' | passwd root >/dev/null 2>&1".format(TEST_PASSWORD))
    _, out = vm.run("for d in /sys/class/net/*; do echo \"${d##*/} $(cat $d/address)\"; done")
    macs = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            macs[parts[1].lower()] = parts[0]
    ifaces = [macs[ml.air_mac(vm.n, k)] for k in range(1, len(vm.air_ports) + 1)]
    cmds = mesh_uci(vm.n, ifaces)
    for i in range(0, len(cmds), 6):
        vm.run(" ; ".join("uci -q " + c for c in cmds[i:i + 6]) + " ; true")
    vm.run("uci commit")

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
    vm.run("/etc/init.d/openmanetd restart", timeout=60)
    wait_for(lambda: vm.run("ip -4 addr show br-ahwlan | grep -q {}".format(ml.node_ip(vm.n)),
                            check=False)[0] == 0, 120)


def guest_fetch(vm, url, timeout=60):
    """Fetch a URL from inside the guest; returns (exit status, body)."""
    return vm.run("wget -q -T 15 --no-check-certificate -O - {} 2>&1".format(url),
                  timeout=timeout, check=False)


def capture_start(vm, name, filt):
    vm.run("rm -f /tmp/{0}.cap; (timeout 90 tcpdump -n -l -A -i br-ahwlan {1} "
           "> /tmp/{0}.cap 2>&1 &)".format(name, ml.shell_quote(filt)))


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

    def originators():
        _, out = first.run("batctl o -H", check=False)
        best = ml.parse_originators(out)
        seen = {ml.node_of_mac(o, count) for o in best}
        return (best, out) if all(n in seen for n in range(2, count + 1)) else None

    got = wait_for(originators, 120)
    best, raw = got if got else ({}, first.run("batctl o -H", check=False)[1])
    res.check("node1 sees every other node as a batman-adv originator", bool(got),
              raw.replace("\n", "; "))
    if count >= 3:
        far = [o for o in best if ml.node_of_mac(o, count) == count]
        hop = ml.node_of_mac(best[far[0]], count) if far else None
        res.check("multi-hop: node1 reaches node{} through node2".format(count), hop == 2,
                  "next hop node{}".format(hop) if hop else raw.replace("\n", "; "))
    for vm in vms[1:]:
        code, out = first.run("ping -c 3 -W 3 {}".format(ml.node_ip(vm.n)), timeout=60, check=False)
        res.check("ping node1 -> node{} across the mesh ({})".format(vm.n, ml.node_ip(vm.n)),
                  code == 0, out.splitlines()[-1] if out else "")
    code, out = last.run("ping -c 3 -W 3 {}".format(ml.node_ip(1)), timeout=60, check=False)
    res.check("ping node{} -> node1 across the mesh".format(count), code == 0,
              out.splitlines()[-1] if out else "")


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
    code, out = guest_fetch(last, "https://{}:8081/".format(ml.node_ip(1)))
    res.check("node{} opens node1's web app over the mesh (https :8081)".format(last.n),
              code == 0 and 'id="root"' in out, out[:200])
    code, out = guest_fetch(last, "http://{}/cgi-bin/luci/".format(ml.node_ip(1)))
    res.check("node{} reaches node1's LuCI over the mesh (:80)".format(last.n),
              e2e.looks_like_luci(out) or "403" in out, out[:200])
    for vm, node in zip(vms, nodes):
        code, body = node.request(node.url("http", 80, "/cgi-bin/luci/"))
        res.check("node{}: LuCI on :80".format(vm.n),
                  code in (200, 403) and e2e.looks_like_luci(body), "HTTP {}".format(code))


def test_atak(res, vms, features):
    first, last = vms[0], vms[-1]
    token = "E2E{}".format(int(time.time()))
    capture_start(last, "sa", "udp and dst port {} and src host {}".format(SA_PORT, ml.node_ip(1)))
    capture_start(last, "chat", "udp and dst port {} and src host {}".format(CHAT_PORT, ml.node_ip(1)))
    time.sleep(3)
    first.run("ip route replace 224.0.0.0/4 dev br-ahwlan")
    for _ in range(3):
        first.run("echo SA-{0} | nc -u -w 1 {1} {2}; echo CHAT-{0} | nc -u -w 1 {3} {4}; true".format(
            token, SA_GROUP, SA_PORT, CHAT_GROUP, CHAT_PORT), timeout=30, check=False)
        time.sleep(1)
    sa = wait_for(lambda: capture_has(last, "sa", "SA-" + token), 20)
    res.check("ATAK SA multicast ({}:{}) from node1 arrives at node{}".format(SA_GROUP, SA_PORT, last.n),
              sa, "")
    chat = wait_for(lambda: capture_has(last, "chat", "CHAT-" + token), 20)
    res.check("ATAK GeoChat multicast ({}:{}) from node1 arrives at node{}".format(
        CHAT_GROUP, CHAT_PORT, last.n), chat, "")

    if "tak" not in features:
        res.skip("node1's own ATAK CoT feed arrives at node{}".format(last.n),
                 "ATAK feed (tak.*) not in this image's openmanetd")
        return
    capture_start(last, "cot", "udp and dst host {} and dst port {} and src host {}".format(
        SA_GROUP, SA_PORT, ml.node_ip(1)))
    got = wait_for(lambda: last.run("grep -c 'UDP' /tmp/cot.cap", check=False)[1].strip() not in ("", "0"),
                   60, interval=5)
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
                            "sha256sum /tmp/atak.apk".format(ml.node_ip(1)), timeout=90, check=False)
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
        test_atak(res, vms, features)
        test_sharing(res, vms, nodes, features, workdir)
        if res.failed():
            error = "failed checks: " + "; ".join(res.failed())
            print("ERROR: " + error, file=sys.stderr, flush=True)
    except Exception as ex:  # noqa: BLE001 - any failure must still write the results
        if isinstance(ex, KeyboardInterrupt):
            raise
        error = "{}: {}".format(type(ex).__name__, ex)
        print("ERROR: " + error, file=sys.stderr, flush=True)
    finally:
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
