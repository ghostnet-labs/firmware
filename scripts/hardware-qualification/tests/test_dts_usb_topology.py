from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dts_usb_topology as topo

ROOT = Path(__file__).resolve().parents[3]
CANDIDATE = Path(__file__).resolve().parents[1] / "cn9130-usb" / "cn9130-clearfog-pro-mainline-usb.dts"

SOC = """/dts-v1/;
/ {
	soc {
		cp0_comphy: phy@120000 {
			cp0_comphy1: phy@1 { reg = <1>; #phy-cells = <1>; };
			cp0_comphy3: phy@3 { reg = <3>; #phy-cells = <1>; };
		};
		cp0_utmi: utmi@580000 {
			status = "disabled"; /* enabled by boards */
			cp0_utmi0: usb-phy@0 { reg = <0>; #phy-cells = <0>; };
			cp0_utmi1: usb-phy@1 { reg = <1>; #phy-cells = <0>; };
		};
		cp0_usb3_0: usb@500000 { reg = <0x500000 0x4000>; status = "disabled"; };
		cp0_usb3_1: usb@510000 { reg = <0x510000 0x4000>; status = "disabled"; };
	};
};
"""

SHIPPED = SOC + """
// mini PCIe, USB 2.0 only
&cp0_usb3_0 { status = "okay"; phys = <&cp0_utmi0>; phy-names = "utmi"; dr_mode = "host"; };
&cp0_usb3_1 {
	status = "okay";
	phys = <&cp0_utmi1>, <&cp0_comphy1 0>;
	phy-names = "utmi", "usb";
	dr_mode = "host";
};
"""

MAINLINE = SOC + """
&cp0_usb3_0 { status = "okay"; phys = <&cp0_comphy1 0>, <&cp0_utmi0>; phy-names = "comphy", "utmi"; dr_mode = "host"; };
&cp0_usb3_1 { status = "okay"; phys = <&cp0_utmi1>; phy-names = "utmi"; dr_mode = "host"; };
"""


def rows_by_label(text):
    rows, notes = topo.topology(topo.parse(text))
    return {r["controller"]: r for r in rows}, notes


class ParserTests(unittest.TestCase):
    def test_label_override_merges_into_node(self):
        rows, _ = rows_by_label(SHIPPED)
        self.assertEqual(rows["cp0_usb3_1"]["node"], "usb@510000")
        self.assertEqual(rows["cp0_usb3_1"]["phys"], ["utmi1", "comphy lane1 port0"])
        self.assertEqual(rows["cp0_usb3_1"]["phy_names"], ["utmi", "usb"])
        self.assertEqual(rows["cp0_usb3_0"]["status"], "okay")

    def test_disabled_default_kept_without_override(self):
        rows, notes = rows_by_label(SOC)
        self.assertEqual(rows["cp0_usb3_0"]["status"], "disabled")
        self.assertEqual(rows["cp0_usb3_0"]["dr_mode"], "(unset)")
        self.assertEqual(notes, [])

    def test_unresolved_label_is_reported_not_dropped(self):
        rows, _ = rows_by_label('/dts-v1/;\n&cp0_usb3_1 { status = "okay"; phys = <&cp0_utmi1>; };\n')
        self.assertEqual(rows["cp0_usb3_1"]["node"], "(include not available)")
        self.assertEqual(rows["cp0_usb3_1"]["status"], "okay")

    def test_orphan_merges_when_definition_arrives_later(self):
        tree = topo.parse('&cp0_usb3_0 { dr_mode = "host"; };\n' + SOC)
        rows, _ = topo.topology(tree)
        self.assertEqual([(r["node"], r["dr_mode"]) for r in rows if r["controller"] == "cp0_usb3_0"],
                         [("usb@500000", "host")])

    def test_compiled_dtc_numeric_cells(self):
        text = '/ { a: x { }; cp0_usb3_0: usb@500000 { phys = <&cp0_comphy1 0x00>, <&cp0_utmi0>; }; };'
        rows, _ = rows_by_label(text)
        self.assertEqual(rows["cp0_usb3_0"]["phys"], ["comphy lane1 port0", "utmi0"])

    def test_delete_property_and_node(self):
        text = SOC + "&cp0_usb3_1 { phys = <&cp0_utmi1>; /delete-property/ phys; };\n/delete-node/ &cp0_usb3_0;\n"
        rows, _ = rows_by_label(text)
        self.assertNotIn("cp0_usb3_0", rows)
        self.assertEqual(rows["cp0_usb3_1"]["phys"], [])

    def test_include_and_comments(self):
        tree = topo.parse('#include "soc.dtsi"\n/* c */ &cp0_usb3_0 { status = "okay"; }; // c\n',
                          include=lambda name: SOC if name == "soc.dtsi" else None)
        rows, _ = topo.topology(tree)
        self.assertEqual(rows[0]["node"], "usb@500000")
        self.assertEqual(rows[0]["status"], "okay")

    def test_malformed_input_raises(self):
        with self.assertRaises(topo.ParseError):
            topo.parse("/ { a { b = <1>; };")
        with self.assertRaises(topo.ParseError):
            topo.parse('/ { a = "unterminated; };')


class RoutingNoteTests(unittest.TestCase):
    def test_shipped_layout_flags_lane_on_other_controller(self):
        _, notes = rows_by_label(SHIPPED)
        self.assertEqual(len(notes), 1)
        self.assertIn("cp0_usb3_1: comphy lane1 port0 selects the lane mux toward USB3 host 0", notes[0])

    def test_mainline_layout_has_no_notes(self):
        _, notes = rows_by_label(MAINLINE)
        self.assertEqual(notes, [])

    def test_port_without_usb_ss_entry(self):
        _, notes = rows_by_label(SOC + "&cp0_usb3_1 { status = \"okay\"; phys = <&cp0_comphy3 0>; };")
        self.assertIn("no USB_HOST_SS entry", notes[0])

    def test_disabled_controller_is_not_flagged(self):
        _, notes = rows_by_label(SOC + "&cp0_usb3_1 { phys = <&cp0_comphy1 0>; };")
        self.assertEqual(notes, [])


class CandidateTests(unittest.TestCase):
    def test_candidate_not_referenced_by_build(self):
        name = CANDIDATE.name
        for top in ("target", "package", "include"):
            for path in (ROOT / top).rglob("*"):
                if path.is_file() and path.suffix in ("", ".mk", ".dts", ".dtsi", ".sh", ".patch"):
                    self.assertNotIn(name, path.read_text(errors="replace"), str(path))

    def test_static_candidate_against_shipped_file(self):
        shipped_dir = ROOT / "target/linux/mvebu/files-6.6/arch/arm64/boot/dts/marvell"
        if not (shipped_dir / "cn9130-clearfog-pro.dts").is_file():
            self.skipTest("shipped CN9130 DTS not present")
        tree, missing = topo.static_tree(CANDIDATE, [str(shipped_dir)])
        rows, notes = topo.topology(tree)
        rows = {r["controller"]: r for r in rows}
        self.assertEqual(missing, ["cn9130.dtsi"])
        self.assertEqual(rows["cp0_usb3_0"]["phys"], ["comphy lane1 port0", "utmi0"])
        self.assertEqual(rows["cp0_usb3_1"]["phys"], ["utmi1"])
        self.assertEqual(rows["cp0_usb3_1"]["usb_phy"], ["cp0_usb3_0_phy1"])
        self.assertEqual(notes, [])

    @unittest.skipUnless(shutil.which("dtc") and shutil.which("cpp"), "dtc/cpp not installed")
    def test_compiled_mode_on_inline_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "soc.dtsi").write_text(SOC.replace("/dts-v1/;", ""))
            board = Path(temp) / "board.dts"
            board.write_text('/dts-v1/;\n#include "soc.dtsi"\n#define OK "okay"\n'
                             "&cp0_usb3_0 { status = OK; phys = <&cp0_comphy1 0>; };\n")
            tree, _ = topo.compiled_tree(board, [])
            rows, notes = topo.topology(tree)
            self.assertEqual(rows[0]["status"], "okay")
            self.assertEqual(rows[0]["phys"], ["comphy lane1 port0"])
            self.assertEqual(notes, [])

    def test_main_static_output(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.dts"
            path.write_text(SHIPPED)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(topo.main(["--static", str(path)]), 0)
                self.assertEqual(topo.main(["--static", str(Path(temp) / "missing.dts")]), 2)
            self.assertIn("| cp0_usb3_1 | usb@510000 | okay", out.getvalue())
            self.assertIn("missing.dts", err.getvalue())


if __name__ == "__main__":
    unittest.main()
