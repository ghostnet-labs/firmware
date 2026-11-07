import datetime
import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import meshlib as ml  # noqa: E402
import mesh_e2e  # noqa: E402


class LineLinksTest(unittest.TestCase):
    def test_three_nodes(self):
        ports, pairs = ml.line_links(3, 41000)
        self.assertEqual(ports, {1: [41001], 2: [41001, 41002], 3: [41002]})
        self.assertEqual(pairs, [(1, 2), (2, 3)])

    def test_two_nodes(self):
        ports, pairs = ml.line_links(2, 50000)
        self.assertEqual(ports, {1: [50001], 2: [50001]})
        self.assertEqual(pairs, [(1, 2)])

    def test_one_node_is_not_a_mesh(self):
        with self.assertRaises(ValueError):
            ml.line_links(1, 41000)


class NmeaTest(unittest.TestCase):
    def test_checksum_of_known_sentence(self):
        # From the NMEA 0183 examples: $GPGLL,4916.45,N,12311.12,W,225444,A*31
        self.assertEqual(ml.nmea_checksum("GPGLL,4916.45,N,12311.12,W,225444,A"), "31")

    def test_fix_sentences(self):
        when = datetime.datetime(2026, 11, 7, 3, 4, 5, tzinfo=datetime.timezone.utc)
        text = ml.nmea_fix(40.5, -112.25, when)
        gga, rmc = text.strip().split("\r\n")
        self.assertTrue(gga.startswith("$GPGGA,030405.00,4030.0000,N,11215.0000,W,1,"))
        self.assertTrue(rmc.startswith("$GPRMC,030405.00,A,4030.0000,N,11215.0000,W,"))
        self.assertIn(",071126,", rmc)
        for s in (gga, rmc):
            body, star = s[1:].split("*")
            self.assertEqual(star, ml.nmea_checksum(body))

    def test_nodes_stand_apart(self):
        self.assertNotEqual(ml.node_position(1), ml.node_position(2))


class YamlTest(unittest.TestCase):
    CFG = "logLevel: info\ntak:\n  enable: false\n  interval: 30s\n# note\nsetup:\n  enabled: true\n"

    def test_drop_block(self):
        self.assertEqual(ml.drop_yaml_block(self.CFG, "tak"),
                         "logLevel: info\n# note\nsetup:\n  enabled: true\n")

    def test_drop_missing_block_is_noop(self):
        self.assertEqual(ml.drop_yaml_block(self.CFG, "weather"), self.CFG)

    def test_drop_does_not_match_prefix(self):
        cfg = "takeover:\n  a: 1\n"
        self.assertEqual(ml.drop_yaml_block(cfg, "tak"), cfg)

    def test_block(self):
        self.assertEqual(
            ml.yaml_block("weather", {"enable": True, "sensor": {"enable": True, "model": "sim"}}),
            "weather:\n  enable: true\n  sensor:\n    enable: true\n    model: sim\n")


class OriginatorsTest(unittest.TestCase):
    TABLE = (
        "   52:54:00:aa:03:01    0.420s (  9.8) 52:54:00:aa:02:01 [      eth1]\n"
        " * 52:54:00:aa:03:01    0.420s ( 25.2) 52:54:00:aa:02:02 [      eth2]\n"
        " * 52:54:00:aa:02:01    0.120s ( 40.1) 52:54:00:aa:02:01 [      eth1]\n"
    )

    def test_best_route_wins(self):
        self.assertEqual(ml.parse_originators(self.TABLE), {
            "52:54:00:aa:03:01": "52:54:00:aa:02:02",
            "52:54:00:aa:02:01": "52:54:00:aa:02:01",
        })

    def test_without_star_first_line_wins(self):
        self.assertEqual(ml.parse_originators(self.TABLE.splitlines()[0]),
                         {"52:54:00:aa:03:01": "52:54:00:aa:02:01"})

    def test_ignores_headers(self):
        self.assertEqual(ml.parse_originators("[B.A.T.M.A.N. adv 2024.3, MainIF/MAC: eth1]\n"), {})

    def test_node_of_mac(self):
        self.assertEqual(ml.node_of_mac("52:54:00:AA:03:02", 3), 3)
        self.assertIsNone(ml.node_of_mac("52:54:00:10:00:01", 3))


def topo(hops=(0, 1, 2), names=("node1", "node2", "node3"), edges=((1, 2), (2, 3))):
    nodes = [{"mac": ml.air_mac(n, 1), "hostname": names[n - 1], "hopsFromSelf": hops[n - 1]}
             for n in (1, 2, 3)]
    nodes[1]["mac"] = ml.air_mac(2, 2)
    nodes[1]["secondaryMacs"] = [ml.air_mac(2, 1)]
    return {"nodes": nodes,
            "edges": [{"fromMac": ml.air_mac(a, 1), "toMac": ml.air_mac(b, 1)} for a, b in edges]}


class TopologyTest(unittest.TestCase):
    PAIRS = [(1, 2), (2, 3)]

    def test_good_line(self):
        found = ml.check_topology(topo(), 3, self.PAIRS)
        self.assertTrue(all(ok for ok, _ in found.values()), found)

    def test_phantom_edge(self):
        found = ml.check_topology(topo(edges=((1, 2), (2, 3), (1, 3))), 3, self.PAIRS)
        self.assertFalse(found["edges"][0])

    def test_missing_node(self):
        t = topo()
        t["nodes"].pop()
        found = ml.check_topology(t, 3, self.PAIRS)
        self.assertFalse(found["nodes"][0])
        self.assertFalse(found["hops"][0])

    def test_unknown_hops(self):
        found = ml.check_topology(topo(hops=(0, 99, 99)), 3, self.PAIRS)
        self.assertFalse(found["hops"][0])
        self.assertIn("node2 99 (want 1)", found["hops"][1])

    def test_missing_hostname(self):
        found = ml.check_topology(topo(names=("node1", "", "node3_eth1")), 3, self.PAIRS)
        self.assertFalse(found["hostnames"][0])
        self.assertIn("node2", found["hostnames"][1])


class ApkTest(unittest.TestCase):
    def test_fake_apk_has_manifest_and_dex(self):
        names = zipfile.ZipFile(io.BytesIO(ml.fake_apk())).namelist()
        self.assertIn("AndroidManifest.xml", names)
        self.assertTrue(any(n.startswith("classes") and n.endswith(".dex") for n in names))

    def test_multipart(self):
        body, ctype = ml.multipart("file", "a.apk", b"PK123")
        boundary = ctype.split("boundary=")[1]
        self.assertTrue(body.startswith(("--" + boundary + "\r\n").encode()))
        self.assertIn(b'name="file"; filename="a.apk"', body)
        self.assertIn(b"\r\n\r\nPK123\r\n--" + boundary.encode() + b"--\r\n", body)


class MentionsTest(unittest.TestCase):
    def test_by_hostname_or_mac(self):
        self.assertTrue(ml.mentions_node([{"hostname": "node1"}], 1))
        self.assertTrue(ml.mentions_node([{"mac": ml.air_mac(1, 1).upper()}], 1))
        self.assertFalse(ml.mentions_node([{"hostname": "node2"}], 1))
        self.assertFalse(ml.mentions_node([], 1))


class ShellQuoteTest(unittest.TestCase):
    def test_single_quote(self):
        self.assertEqual(ml.shell_quote("it's"), "'it'\\''s'")


class MeshConfigTest(unittest.TestCase):
    def test_uci_has_one_hardif_per_link(self):
        cmds = mesh_e2e.mesh_uci(2, ["eth1", "eth2"])
        self.assertIn("set network.batmesh0.device=eth1", cmds)
        self.assertIn("set network.batmesh1.device=eth2", cmds)
        self.assertIn("set network.ahwlan.ipaddr=10.41.1.2", cmds)
        self.assertIn("set network.bat0.routing_algo=BATMAN_V", cmds)
        self.assertIn("set gpsd.core.device=/dev/ttyS1", cmds)

    def test_overrides_follow_features(self):
        self.assertEqual(mesh_e2e.openmanetd_overrides(1, set()), {})
        full = mesh_e2e.openmanetd_overrides(1, {"tak", "coverage", "weather"})
        self.assertEqual(full["tak"]["coverage"], {"enable": True})
        self.assertEqual(full["weather"]["sensor"]["model"], "sim")
        self.assertNotIn("weather", mesh_e2e.openmanetd_overrides(2, {"tak", "weather"}))

    def test_vm_command_line(self):
        vm = mesh_e2e.MeshVM(2, "/d/disk.img", {80: 1, 8080: 2, 8081: 3}, [41001, 41002], "kvm", "/d")
        cmd = " ".join(vm.cmd())
        self.assertIn("mac=" + ml.lan_mac(2), cmd)
        self.assertIn("socket,id=air1,mcast=230.0.0.1:41001", cmd)
        self.assertIn("mac=" + ml.air_mac(2, 2), cmd)
        self.assertIn("hostfwd=tcp:127.0.0.1:3-10.99.0.15:8081", cmd)
        self.assertEqual(cmd.count("-serial"), 2)


if __name__ == "__main__":
    unittest.main()
