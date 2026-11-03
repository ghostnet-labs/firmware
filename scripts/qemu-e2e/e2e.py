#!/usr/bin/env python3
"""Boot an OpenMANET x86_64 image in QEMU and test first boot, LuCI and OTA.

Usage:
    e2e.py --image openmanet-...-x86-64-generic-squashfs-combined.img.gz
           [--workdir DIR] [--accel auto|kvm|tcg] [--skip-ota]

What it checks (decisions D-037, D-038, D-039):
  1. First boot: openmanetd serves the UI on https://<node>:8081 (and plain
     http on :8080), the setup wizard is enabled and setup is not complete,
     and PAM sign-in works for root once a password is set.
  2. LuCI: reachable directly on :80, not proxied while
     frontend.luciProxy.enable is false (the image default), and served
     through https://<node>:8081/cgi-bin/luci/ once it is turned on.
  3. OTA: the online update check finds a newer release in
     ghostnet-labs/firmware (served by a local stand-in for the GitHub API),
     downloads and verifies the image against the release's sha256sums,
     runs sysupgrade, and the node boots the new image with its settings
     kept and its non-config overlay files gone.

The guest has no HaLow radio, so the wizard itself cannot be applied here;
that stays a bench check on real hardware (GHO-75).

Needs qemu-system-x86_64, openssl and gzip on the host. Python stdlib only.
Writes console.log, results.json and summary.md to the work directory.
"""

import argparse
import gzip
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from console import Console, ConsoleError  # noqa: E402
import fakegithub  # noqa: E402

# OpenMANET images put LAN (br-lan) at 10.41.254.1/16
# (boards/common/general_diffconfig), not OpenWrt's 192.168.1.1.
GUEST_IP = "10.41.254.1"
GUEST_NET = "10.41.0.0/16"
HOST_IP = "10.41.0.2"     # QEMU user networking: the host's loopback
RELEASES_REPO = "ghostnet-labs/firmware"
TEST_PASSWORD = "e2e-Passw0rd"
KEPT_HOSTNAME = "e2e-kept"
DROPPED_MARKER = "/usr/lib/e2e-overlay-marker"
BOOT_TIMEOUT = {"kvm": 300, "tcg": 900}


class CheckFailed(AssertionError):
    pass


class Results:
    def __init__(self):
        self.items = []

    def check(self, name, ok, detail="", fatal=False):
        """Record a check. A failed fatal check stops the run (later
        checks depend on it); other failures are reported at the end."""
        detail = str(detail)[:400]
        self.items.append({"name": name, "ok": bool(ok), "detail": detail})
        print("[{}] {}{}".format("PASS" if ok else "FAIL", name,
                                 " - " + detail if detail else ""), flush=True)
        if not ok and fatal:
            raise CheckFailed("{}: {}".format(name, detail))

    def failed(self):
        return [it["name"] for it in self.items if it["ok"] is False]

    def note(self, name, detail):
        self.items.append({"name": name, "ok": None, "detail": str(detail)})
        print("[INFO] {} - {}".format(name, detail), flush=True)

    def write(self, workdir, error=None):
        with open(os.path.join(workdir, "results.json"), "w") as f:
            json.dump({"checks": self.items, "error": error}, f, indent=2)
        lines = ["## QEMU first boot and OTA test", ""]
        lines += ["| Result | Check | Detail |", "|---|---|---|"]
        for it in self.items:
            mark = {True: "pass", False: "FAIL", None: "info"}[it["ok"]]
            detail = it["detail"].replace("|", "\\|").replace("\n", " ")[:300]
            lines.append("| {} | {} | {} |".format(mark, it["name"], detail))
        if error:
            lines += ["", "**Stopped:** " + error.replace("\n", " ")[:600]]
        text = "\n".join(lines) + "\n"
        with open(os.path.join(workdir, "summary.md"), "w") as f:
            f.write(text)
        step = os.environ.get("GITHUB_STEP_SUMMARY")
        if step:
            with open(step, "a") as f:
                f.write(text)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def gunzip(src, dst):
    """OpenWrt pads its .img.gz files; gzip(1) warns about trailing garbage."""
    with open(dst, "wb") as out:
        proc = subprocess.run(["gzip", "-dc", src], stdout=out, stderr=subprocess.PIPE)
    if proc.returncode not in (0, 2) or os.path.getsize(dst) == 0:
        raise RuntimeError("gunzip failed: " + proc.stderr.decode())


class Node:
    """HTTP access to the guest through QEMU host forwards."""

    def __init__(self, ports):
        self.ports = ports
        self.token = None
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE  # the node's cert is self-signed

    def url(self, scheme, guest_port, path):
        return "{}://127.0.0.1:{}{}".format(scheme, self.ports[guest_port], path)

    def request(self, url, data=None, headers=None, timeout=20, method=None, retry_for=60):
        """One HTTP request; connection errors are retried for `retry_for`
        seconds, since services come up a little after the console does."""
        deadline = time.monotonic() + retry_for
        while True:
            req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=self._ctx) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")
            except (OSError, urllib.error.URLError):
                if time.monotonic() > deadline:
                    raise
                time.sleep(2)

    def ui(self, path):
        return self.request(self.url("https", 8081, path))

    def rpc(self, method, body=None, timeout=30, retry_for=60):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        status, text = self.request(
            self.url("https", 8081, "/rpc/" + method),
            data=json.dumps(body or {}).encode(), headers=headers, timeout=timeout,
            retry_for=retry_for)
        try:
            return status, json.loads(text) if text else {}
        except ValueError:
            return status, {"raw": text}

    def login(self, password):
        status, text = self.request(
            self.url("https", 8081, "/auth/login"),
            data=json.dumps({"username": "root", "password": password}).encode(),
            headers={"Content-Type": "application/json"})
        if status == 200:
            self.token = json.loads(text).get("token")
        return status, text

    def wait_ready(self, timeout):
        """Wait until openmanetd answers GetSetupStatus (no auth needed)."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                status, body = self.rpc("openmanet.setup.v1.SetupService/GetSetupStatus", retry_for=0)
                if status == 200:
                    return body
                last = "HTTP {}".format(status)
            except (OSError, urllib.error.URLError) as e:
                last = str(e)
            time.sleep(3)
        raise CheckFailed("openmanetd not answering on :8081 after {}s ({})".format(timeout, last))


class VM:
    """The QEMU process plus its serial console.

    QEMU runs with -no-reboot: a guest reboot ends the process and the test
    starts it again on the same disk, like a power cycle. (A warm reset under
    QEMU hung in GRUB after a sysupgrade rewrote the boot partition, on both
    the pc and q35 machines.) The console methods act on the current boot.
    """

    def __init__(self, disk, ports, accel, workdir):
        self.disk = disk
        self.ports = ports
        self.accel = accel
        self.workdir = workdir
        self.sock = os.path.join(workdir, "console.sock")
        self.proc = None
        self.con = None
        self.boots = 0

    def cmd(self):
        fwd = ",".join("hostfwd=tcp:127.0.0.1:{}-{}:{}".format(h, GUEST_IP, g)
                       for g, h in self.ports.items())
        cmd = [
            "qemu-system-x86_64", "-machine", "q35", "-m", "2048", "-smp", "2",
            "-accel", self.accel, "-display", "none", "-monitor", "none",
            "-no-user-config", "-no-reboot",
            "-drive", "file={},format=raw,if=virtio".format(self.disk),
            "-netdev", "user,id=lan,net={},host={},dhcpstart=10.41.0.100,{}".format(
                GUEST_NET, HOST_IP, fwd),
            "-device", "virtio-net-pci,netdev=lan",
            "-chardev", "socket,id=con,path={},server=on,wait=off".format(self.sock),
            "-serial", "chardev:con",
        ]
        if self.accel == "kvm":
            cmd[1:1] = ["-cpu", "host"]
        return cmd

    def start(self):
        if os.path.exists(self.sock):
            os.remove(self.sock)
        self.boots += 1
        cmd = self.cmd()
        print("+ " + " ".join(cmd), flush=True)
        log = open(os.path.join(self.workdir, "qemu.log"), "a")
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        log.close()
        self.con = Console(self.sock, os.path.join(self.workdir, "console.log"))

    def power_cycle_after_reboot(self, timeout):
        """Wait for the guest to reboot (QEMU exits) and boot it again."""
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise ConsoleError("guest did not reboot within {}s".format(timeout))
        self.con.close()
        self.start()

    def stop(self):
        if self.con:
            self.con.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def run(self, *a, **kw):
        return self.con.run(*a, **kw)

    def expect(self, *a, **kw):
        return self.con.expect(*a, **kw)

    def mark(self):
        self.con.mark()

    def login(self, timeout):
        self.con.login(timeout)


def pick_accel(choice):
    if choice != "auto":
        return choice
    return "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"


def test_first_boot(res, node, con, status):
    res.check("openmanetd answers on https :8081", True, "GetSetupStatus HTTP 200")
    code, body = node.ui("/")
    res.check("UI shell served on https :8081/", code == 200 and 'id="root"' in body,
              "HTTP {}".format(code))
    code, body = node.ui("/setup")
    res.check("wizard route /setup served", code == 200 and 'id="root"' in body,
              "HTTP {}".format(code))
    code, _ = node.request(node.url("http", 8080, "/"))
    res.check("UI also served on plain http :8080", code == 200, "HTTP {}".format(code))
    res.check("setup wizard enabled on a fresh image (setup.enabled)",
              status.get("isEnabled") is True, status)
    res.check("setup not complete on a fresh image",
              not status.get("isSetupComplete"), status.get("isSetupComplete"))
    res.check("fresh node not reported as already configured (wizard is offered, not optional)",
              not status.get("alreadyConfigured"), status.get("alreadyConfigured"))
    res.note("HaLow radio present", "{} (QEMU has none, so the browser shows the "
             "no-HaLow step instead of the wizard)".format(status.get("hasHalowRadio", False)))

    code, body = node.rpc("openmanet.sysupgrade.v1.SysupgradeService/GetSystemInfo")
    res.check("authenticated RPCs refuse a caller with no session", code == 401,
              "HTTP {}".format(code))
    con.run("printf '%s\\n%s\\n' '{0}' '{0}' | passwd root >/dev/null 2>&1".format(TEST_PASSWORD))
    code, text = node.login(TEST_PASSWORD)
    res.check("PAM sign-in as root works", code == 200 and node.token, "HTTP {}".format(code), fatal=True)
    code, body = node.request(node.url("https", 8081, "/auth/check"),
                              headers={"Authorization": "Bearer " + node.token})
    res.check("session is accepted", code == 200 and '"authenticated":true' in body.replace(" ", ""),
              body[:200])


LUCI_MARKERS = ("luci-static", "cgi-bin/luci")


def looks_like_luci(body):
    return 'id="root"' not in body and any(m in body for m in LUCI_MARKERS)


def test_luci(res, node, con):
    code, body = node.request(node.url("http", 80, "/cgi-bin/luci/"))
    res.check("LuCI reachable directly on :80", code in (200, 403) and looks_like_luci(body),
              "HTTP {}".format(code))
    code, body = node.ui("/cgi-bin/luci/")
    res.check("LuCI not proxied while frontend.luciProxy.enable is false (image default)",
              code == 200 and 'id="root"' in body, "HTTP {}".format(code))
    con.run("sed -i '/^  luciProxy:/,/enable:/ s/enable: false/enable: true/' "
            "/etc/openmanetd/config.yml && grep -A1 '^  luciProxy:' /etc/openmanetd/config.yml")
    deadline = time.monotonic() + 60
    while True:
        code, body = node.ui("/cgi-bin/luci/")
        if looks_like_luci(body) or time.monotonic() > deadline:
            break
        time.sleep(3)
    res.check("LuCI served through https :8081/cgi-bin/luci/ once the proxy is on",
              code in (200, 403) and looks_like_luci(body), "HTTP {}".format(code))
    code, body = node.ui("/luci-static/resources/luci.js")
    res.check("LuCI static files proxied", code == 200 and "luci" in body.lower()[:4000],
              "HTTP {}".format(code))
    code, body = node.request(node.url("https", 8081, "/api/system/info"),
                              headers={"Authorization": "Bearer " + node.token})
    enabled = '"luci_proxy_enabled":true' in body.replace(" ", "")
    res.check("UI reports the Advanced (LuCI) entry enabled", code == 200 and enabled,
              "HTTP {}".format(code))


def bump_version(version):
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version or "")
    if not m:
        return None
    return "v{}.0.0".format(int(m.group(1)) + 1)


def test_ota(res, node, con, image, workdir):
    code, body = node.rpc("openmanet.sysupgrade.v1.SysupgradeService/GetSystemInfo")
    res.check("GetSystemInfo answers", code == 200, body if code != 200 else "", fatal=True)
    info = body.get("info", {})
    res.note("system", "board {} target {} version {} rootfs {}".format(
        info.get("boardName"), info.get("target"), info.get("openmanetVersion"),
        info.get("rootfsType")))
    res.check("node can sysupgrade", info.get("sysupgradeCapable") is True,
              info.get("sysupgradeCapableReason", ""), fatal=True)
    current = info.get("openmanetVersion", "")
    tag = bump_version(current)
    res.check("current OpenMANET version is known", tag is not None, current, fatal=True)

    # A fake release one major version ahead, carrying this same image.
    asset = os.path.basename(image)
    staged = os.path.join(workdir, "release")
    os.makedirs(staged, exist_ok=True)
    img = os.path.join(staged, asset)
    shutil.copyfile(image, img)
    sums = os.path.join(staged, "sha256sums")
    with open(sums, "w") as f:
        f.write(fakegithub.sha256sums_text([(asset, fakegithub.sha256_file(img))]))
    manifest = os.path.join(staged, "manifest.json")
    with open(manifest, "w") as f:
        f.write(fakegithub.manifest_json(info.get("boardName", ""), asset))
    ca, cert, key = fakegithub.make_tls_material(workdir)
    files = {(tag, asset): img, (tag, "sha256sums"): sums, (tag, "manifest.json"): manifest}
    fake = fakegithub.FakeGitHub(RELEASES_REPO, [], files, workdir)
    tls_port = fake.serve(free_port(), tls=(cert, key))
    http_port = fake.serve(free_port())
    fake.releases = fakegithub.release_json(
        tag, {asset: img, "sha256sums": sums, "manifest.json": manifest},
        "https://api.github.com")

    # Point api.github.com at the stand-in: hosts entry, trusted CA, and a
    # DNAT from :443 to the server's port on the host.
    con.run("wget -q -O /tmp/e2e-ca.pem http://{}:{}/files/ca.pem".format(HOST_IP, http_port))
    con.run("mkdir -p /etc/ssl/certs && cat /tmp/e2e-ca.pem >> /etc/ssl/certs/ca-certificates.crt")
    con.run("echo '{} api.github.com' >> /etc/hosts".format(HOST_IP))
    con.run("nft add table ip e2e && "
            "nft 'add chain ip e2e out {{ type nat hook output priority -100 ; }}' && "
            "nft add rule ip e2e out ip daddr {0} tcp dport 443 dnat to {0}:{1}".format(HOST_IP, tls_port))
    _, repo = con.run("sed -n '/^sysupgrade:/,/^[^ ]/ s/^  releasesRepo: *//p' /etc/openmanetd/config.yml")
    res.check("image checks ghostnet-labs/firmware for updates (D-039)",
              repo.strip() == RELEASES_REPO, repo.strip())
    # The CA bundle and the config are read at startup.
    con.run("/etc/init.d/openmanetd restart")
    time.sleep(5)
    node.wait_ready(240)
    code, _ = node.login(TEST_PASSWORD)
    res.check("sign-in after restart", code == 200, "HTTP {}".format(code), fatal=True)

    code, body = node.rpc("openmanet.sysupgrade.v1.SysupgradeService/ListAvailableUpdates",
                          {"forceRefresh": True, "includePrerelease": False}, timeout=60)
    updates = body.get("updates", []) if code == 200 else []
    match = [u for u in updates if u.get("release", {}).get("tag") == tag
             or u.get("release", {}).get("tagName") == tag]
    res.check("online check lists the newer release", code == 200 and match,
              json.dumps(body)[:400], fatal=True)
    res.check("release API was called for the configured repo",
              fake.requested("/repos/{}/releases".format(RELEASES_REPO)), "")
    matched = match[0].get("matchedAsset", {}) if match else {}
    res.check("release image matched to this board (manifest.json)",
              matched.get("name") == asset, matched, fatal=True)

    # Settings that must survive, and an overlay file that must not.
    con.run("uci set system.@system[0].hostname={} && uci commit system && "
            "touch {}".format(KEPT_HOSTNAME, DROPPED_MARKER))
    con.mark()
    code, body = node.rpc("openmanet.sysupgrade.v1.SysupgradeService/StartUpgrade",
                          {"releaseTag": tag, "assetName": ""})
    res.check("StartUpgrade accepted", code == 200, json.dumps(body)[:300], fatal=True)

    phases = []
    event = {}
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        try:
            code, st = node.rpc("openmanet.sysupgrade.v1.SysupgradeService/GetUpgradeStatus",
                               timeout=10, retry_for=0)
        except (OSError, urllib.error.URLError):
            break  # node went down for the flash
        event = st.get("event", {}) if code == 200 else {}
        phase = event.get("phase", "")
        if phase and (not phases or phases[-1] != phase):
            phases.append(phase)
        if phase == "PHASE_FAILED":
            break
        if phase == "PHASE_UPGRADING":
            break
        time.sleep(2)
    res.note("upgrade phases seen", " -> ".join(phases))
    handed_off = "PHASE_UPGRADING" in phases
    if not handed_off and "PHASE_FAILED" not in phases:
        # Under KVM the flash can start between two polls: openmanetd is
        # killed before it reports PHASE_UPGRADING. sysupgrade's own console
        # lines are then the proof the verified image was handed over.
        try:
            con.expect(r"upgrade: (?:Switching to ramdisk|Performing system upgrade)", 120)
            handed_off = True
            res.note("hand-off seen on the console", "sysupgrade started")
        except ConsoleError:
            pass
    res.check("image downloaded, verified and handed to sysupgrade",
              handed_off,
              event.get("error") if "PHASE_FAILED" in phases else phases, fatal=True)
    res.check("image fully downloaded from the release",
              fake.requested("/download/{}/{}".format(tag, asset)), "")

    con.expect(r"upgrade: Rebooting system", 900)
    con.power_cycle_after_reboot(120)
    con.login(BOOT_TIMEOUT[con.accel])
    res.check("node rebooted into the new image", True)
    status = node.wait_ready(300)
    res.check("openmanetd back up after the upgrade", True)
    _, host = con.run("uci get system.@system[0].hostname")
    res.check("settings kept across the upgrade (hostname)", host.strip() == KEPT_HOSTNAME, host)
    st_code, _ = con.run("test -e {}".format(DROPPED_MARKER), check=False)
    res.check("new rootfs flashed (non-config overlay file gone)", st_code != 0, DROPPED_MARKER)
    code, _ = node.login(TEST_PASSWORD)
    res.check("root password kept across the upgrade", code == 200, "HTTP {}".format(code))
    _, proxy = con.run("grep -A1 '^  luciProxy:' /etc/openmanetd/config.yml")
    res.check("openmanetd config kept across the upgrade", "enable: true" in proxy, proxy)
    res.check("setup state unchanged by the upgrade",
              status.get("isEnabled") is True and not status.get("isSetupComplete"),
              "setup state unchanged: {}".format({k: status.get(k) for k in ("isEnabled", "isSetupComplete")}))
    fake.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--image", required=True, help="x86_64 squashfs combined .img.gz")
    ap.add_argument("--workdir", help="where logs and the disk go (default: a temp dir)")
    ap.add_argument("--accel", default="auto", choices=["auto", "kvm", "tcg"])
    ap.add_argument("--skip-ota", action="store_true", help="stop after the LuCI checks")
    ap.add_argument("--boot-timeout", type=int, default=0,
                    help="seconds to wait for the console (default 300 kvm, 900 tcg)")
    args = ap.parse_args(argv)

    workdir = args.workdir or tempfile.mkdtemp(prefix="qemu-e2e-")
    os.makedirs(workdir, exist_ok=True)
    image = os.path.abspath(args.image)
    accel = pick_accel(args.accel)
    boot_timeout = args.boot_timeout or BOOT_TIMEOUT[accel]
    print("work dir: {}  accel: {}".format(workdir, accel), flush=True)

    disk = os.path.join(workdir, "disk.img")
    gunzip(image, disk)
    ports = {80: free_port(), 8080: free_port(), 8081: free_port()}
    res = Results()
    vm = VM(disk, ports, accel, workdir)
    error = None
    try:
        vm.start()
        vm.login(boot_timeout)
        _, rel = vm.run("grep DISTRIB_DESCRIPTION /etc/openwrt_release")
        res.note("image", rel.split("=", 1)[-1].strip("'\""))
        node = Node(ports)
        status = node.wait_ready(300)
        test_first_boot(res, node, vm, status)
        test_luci(res, node, vm)
        if not args.skip_ota:
            test_ota(res, node, vm, image, workdir)
        if res.failed():
            error = "failed checks: " + "; ".join(res.failed())
            print("ERROR: " + error, file=sys.stderr, flush=True)
    except Exception as e:  # noqa: BLE001 - any failure must still write the results
        if isinstance(e, KeyboardInterrupt):
            raise
        error = "{}: {}".format(type(e).__name__, e)
        print("ERROR: " + error, file=sys.stderr, flush=True)
        if vm.con:
            try:
                _, logs = vm.run("logread | grep -i openmanetd | tail -n 60", timeout=20, check=False)
                with open(os.path.join(workdir, "openmanetd-logread.txt"), "w") as f:
                    f.write(logs)
            except (ConsoleError, OSError):
                pass
    finally:
        res.write(workdir, error)
        vm.stop()
        try:
            os.remove(disk)
        except OSError:
            pass
    return 1 if error else 0


if __name__ == "__main__":
    sys.exit(main())
