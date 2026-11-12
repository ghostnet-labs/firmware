"""Pure helpers for mesh_e2e.py: topology layout, NMEA, YAML edits, parsers.

Nothing here touches QEMU or the network, so tests/test_meshlib.py covers
it directly. Python standard library only.
"""

import io
import json
import re
import uuid
import zipfile

AHWLAN_PREFIX = "10.41.1."   # node N gets 10.41.1.N/16 on br-ahwlan
MGMT_GUEST_IP = "10.99.0.15"  # br-lan moves here, behind QEMU user net
MGMT_NET = "10.99.0.0/24"
MGMT_HOST_IP = "10.99.0.2"

# A spot to stand the nodes at: a line running east, ~400 m apart.
BASE_LAT = 40.3300
BASE_LON = -112.0100
STEP_LON = 0.0047


def node_ip(n):
    return AHWLAN_PREFIX + str(n)


def lan_mac(n):
    return "52:54:00:10:00:{:02x}".format(n)


def air_mac(n, k):
    """MAC of node n's k-th (1-based) air interface."""
    return "52:54:00:aa:{:02x}:{:02x}".format(n, k)


def line_links(count, base_port):
    """Air links for `count` nodes in a line: node i and i+1 share link i.

    Returns {node: [port, ...]} in interface order, and the list of
    (a, b) neighbour pairs. Each link is its own QEMU multicast segment,
    so only neighbours hear each other and node 1 reaches node 3 only
    through node 2.
    """
    if count < 2:
        raise ValueError("a mesh needs at least two nodes")
    ports = {n: [] for n in range(1, count + 1)}
    pairs = []
    for i in range(1, count):
        port = base_port + i
        ports[i].append(port)
        ports[i + 1].append(port)
        pairs.append((i, i + 1))
    return ports, pairs


def nmea_checksum(body):
    c = 0
    for ch in body:
        c ^= ord(ch)
    return "{:02X}".format(c)


def _nmea_coord(value, lat):
    hemi = ("N" if value >= 0 else "S") if lat else ("E" if value >= 0 else "W")
    value = abs(value)
    deg = int(value)
    minutes = (value - deg) * 60
    if lat:
        return "{:02d}{:07.4f}".format(deg, minutes), hemi
    return "{:03d}{:07.4f}".format(deg, minutes), hemi


def nmea_fix(lat, lon, when, alt_m=1480.0):
    """GGA + RMC sentences for a 3D fix at (lat, lon) at UTC `when`."""
    hms = when.strftime("%H%M%S") + ".00"
    dmy = when.strftime("%d%m%y")
    la, ns = _nmea_coord(lat, True)
    lo, ew = _nmea_coord(lon, False)
    gga = "GPGGA,{},{},{},{},{},1,08,0.9,{:.1f},M,-17.0,M,,".format(hms, la, ns, lo, ew, alt_m)
    rmc = "GPRMC,{},A,{},{},{},{},0.0,0.0,{},,,A".format(hms, la, ns, lo, ew, dmy)
    return "".join("${}*{}\r\n".format(s, nmea_checksum(s)) for s in (gga, rmc))


def node_position(n):
    return BASE_LAT, BASE_LON + (n - 1) * STEP_LON


def drop_yaml_block(text, key):
    """Remove a top-level `key:` block (and everything indented under it)."""
    out = []
    skipping = False
    for line in text.splitlines(True):
        if line.strip() and not line[0].isspace():
            # A top-level key (or a top-level comment) ends the block.
            skipping = line.startswith(key + ":")
        if not skipping:
            out.append(line)
    return "".join(out)


def yaml_block(key, values):
    """A two-level YAML block from a dict (nested dicts become sub-blocks)."""
    lines = [key + ":"]

    def emit(d, indent):
        for k, v in d.items():
            if isinstance(v, dict):
                lines.append(" " * indent + k + ":")
                emit(v, indent + 2)
            else:
                if isinstance(v, bool):
                    v = "true" if v else "false"
                lines.append(" " * indent + "{}: {}".format(k, v))

    emit(values, 2)
    return "\n".join(lines) + "\n"


def shell_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def parse_originators(text):
    """`batctl o -n -H` (BATMAN_V or IV) -> {originator: next_hop}, best route only.

    Lines look like
      ' * 52:54:00:aa:03:01    0.420s (  9.8) 52:54:00:aa:02:02 [  eth2]'
    A '*' marks the best route; when no line for an originator has one,
    the first line wins. Without -n batctl prints bat-hosts names instead
    of MACs, and those lines are skipped.
    """
    mac = r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}"
    rx = re.compile(r"^\s*(\*)?\s*({0})\s.*?\s({0})\s*\[".format(mac))
    best = {}
    for line in text.splitlines():
        m = rx.match(line.lower())
        if not m:
            continue
        star, orig, nh = m.group(1), m.group(2), m.group(3)
        if star or orig not in best:
            best[orig] = nh
    return best


def node_of_mac(mac, count, max_ifaces=4):
    for n in range(1, count + 1):
        for k in range(1, max_ifaces + 1):
            if air_mac(n, k) == mac.lower():
                return n
    return None


def check_topology(topo, count, pairs, self_node=1):
    """Compare a GetMeshTopology answer with the line we built.

    Returns a dict of findings (each a (ok, detail) pair) so the caller
    can report them as separate checks.
    """
    nodes = topo.get("nodes", [])
    edges = topo.get("edges", [])
    seen = {}
    for nd in nodes:
        if nd.get("isSelf"):
            # The serving node may be keyed by its bat0 MAC, not a radio's.
            seen[self_node] = nd
            continue
        macs = [nd.get("mac", "")] + list(nd.get("secondaryMacs") or [])
        for m in macs:
            n = node_of_mac(m, count)
            if n:
                seen[n] = nd
                break
    found = {}
    found["nodes"] = (len(seen) == count,
                      "{} of {} nodes (got {} entries)".format(len(seen), count, len(nodes)))

    self_macs = {}
    if self_node in seen:
        nd = seen[self_node]
        for m in [nd.get("mac", "")] + list(nd.get("secondaryMacs") or []):
            self_macs[m.lower()] = self_node
    want = {tuple(sorted(p)) for p in pairs}
    got = set()
    unknown = 0
    for e in edges:
        a = self_macs.get(e.get("fromMac", "").lower()) or node_of_mac(e.get("fromMac", ""), count)
        b = self_macs.get(e.get("toMac", "").lower()) or node_of_mac(e.get("toMac", ""), count)
        if a and b and a != b:
            got.add(tuple(sorted((a, b))))
        else:
            unknown += 1
    found["edges"] = (got == want and unknown == 0,
                      "edges {} want {}{}".format(sorted(got), sorted(want),
                                                 ", {} unmatched".format(unknown) if unknown else ""))

    bad_hops = []
    for n, nd in sorted(seen.items()):
        want_hops = abs(n - self_node)
        got_hops = nd.get("hopsFromSelf") or 0  # proto3 JSON omits zero
        if got_hops != want_hops:
            bad_hops.append("node{} {} (want {})".format(n, got_hops, want_hops))
    found["hops"] = (not bad_hops and len(seen) == count,
                     "; ".join(bad_hops) or "all hop counts match the line")

    bad_names = []
    for n, nd in sorted(seen.items()):
        name = nd.get("hostname", "")
        if not name.startswith("node{}".format(n)):
            bad_names.append("node{} shown as {!r}".format(n, name))
    found["hostnames"] = (not bad_names and len(seen) == count,
                          "; ".join(bad_names) or "every node named")
    return found


def fake_apk():
    """A minimal zip that passes openmanetd's APK check (manifest + dex)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("AndroidManifest.xml", b"\x03\x00\x08\x00e2e-test-manifest")
        z.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 64)
        z.writestr("res/e2e.txt", "mesh e2e test package, not a real app\n")
    return buf.getvalue()


def multipart(field, filename, data, content_type="application/vnd.android.package-archive"):
    """Encode one file as multipart/form-data. Returns (body, content_type)."""
    boundary = "e2e" + uuid.uuid4().hex
    head = ("--{b}\r\nContent-Disposition: form-data; name=\"{f}\"; filename=\"{n}\"\r\n"
            "Content-Type: {t}\r\n\r\n").format(b=boundary, f=field, n=filename, t=content_type)
    body = head.encode() + data + "\r\n--{}--\r\n".format(boundary).encode()
    return body, "multipart/form-data; boundary=" + boundary


def mentions_node(obj, n):
    """True when the JSON form of `obj` names node n (hostname or MAC)."""
    text = json.dumps(obj).lower()
    if '"node{}'.format(n) in text or "node{}_".format(n) in text:
        return True
    return any(air_mac(n, k) in text for k in range(1, 5))


def lua_mcast_send(group, port, text):
    """A lua one-liner (nixio ships with LuCI) that sends one UDP datagram;
    busybox nc in the image has no UDP mode."""
    return ("lua -e 'local n=require\"nixio\"; local s=n.socket(\"inet\",\"dgram\"); "
            "s:setopt(\"ip\",\"multicast_ttl\",4); "
            "assert(s:sendto(\"{t}\",\"{g}\",{p}))'").format(t=text, g=group, p=port)


# What openmanetd's address reservation is meant to change in the network
# config: the mesh interface's address (and its IPv6 options) and the
# bootstrap 'lan' section it deletes.
RESERVATION_CHANGES = ("network.ahwlan.ipaddr=", "network.ahwlan.ip6", "network.lan.", "network.lan=")


def lost_uci_lines(before, after):
    """`uci show` lines present before but gone after, other than the
    ones address reservation is meant to change."""
    have = set(l.strip() for l in after.splitlines())
    lost = []
    for line in before.splitlines():
        line = line.strip()
        if not line or line in have or line.startswith(RESERVATION_CHANGES):
            continue
        lost.append(line)
    return lost


def _uci_values(shown):
    vals = {}
    for line in shown.splitlines():
        if "=" not in line:
            continue
        key, val = line.strip().split("=", 1)
        vals[key] = re.findall(r"'([^']*)'", val) or [val]
    return vals


def missing_uci(cmds, shown):
    """The `set`/`add_list` commands whose value is not in `uci show` output.
    Anonymous (@type[n]) sections and deletes are not checked."""
    vals = _uci_values(shown)
    missing = []
    for c in cmds:
        verb, _, rest = c.partition(" ")
        if verb not in ("set", "add_list") or "=" not in rest or "@" in rest:
            continue
        key, val = rest.split("=", 1)
        have = vals.get(key, [])
        if val not in have:
            missing.append(c)
    return missing
