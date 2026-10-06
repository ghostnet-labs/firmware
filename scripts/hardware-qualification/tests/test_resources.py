import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resources import evaluate


def phy(name, qualified=True):
    return {"id": name, "available": True, "qualified": qualified, "bands": ["5"],
            "combinations": [{"max_channels": 1, "max_interfaces": 3,
                              "limits": [{"roles": ["mesh", "ap"], "max": 2},
                                         {"roles": ["station"], "max": 1}]}]}


def radio(name, freq=5180, role="mesh"):
    return {"id": name, "band": "5", "center_mhz": freq, "width_mhz": 20, "role": role}


def window(name, low, high, services=None):
    return {"id": name, "available": True, "qualified": True, "low_mhz": low,
            "high_mhz": high, "services": services or ["scanner"], "max_demodulators": 4}


def rx(name, center, width=0.0125, service="scanner"):
    return {"id": name, "service": service, "center_mhz": center, "width_mhz": width}


class ResourceTests(unittest.TestCase):
    def test_three_independent_channels_cannot_fit_two_phys(self):
        plan = {"schema_version": 1, "phys": [phy("p0"), phy("p1")],
                "radio_demands": [radio("old", 5180), radio("new", 5200), radio("eud", 5220, "ap")]}
        self.assertEqual(evaluate(plan)["status"], "infeasible")
        plan["phys"].append(phy("p2"))
        self.assertEqual(evaluate(plan)["status"], "feasible")

    def test_same_channel_sharing_needs_qualified_interface_combination(self):
        plan = {"schema_version": 1, "phys": [phy("p")],
                "radio_demands": [radio("mesh"), radio("eud", role="ap")]}
        self.assertEqual(evaluate(plan)["status"], "feasible")
        plan["phys"][0]["combinations"][0]["limits"][0]["max"] = 1
        self.assertEqual(evaluate(plan)["status"], "infeasible")

    def test_channel_width_is_part_of_channel_assignment(self):
        plan = {"schema_version": 1, "phys": [phy("p")],
                "radio_demands": [radio("a"), radio("b", role="ap")]}
        plan["radio_demands"][1]["width_mhz"] = 40
        self.assertEqual(evaluate(plan)["status"], "infeasible")

    def test_candidates_fail_closed_and_disabled_never_used(self):
        plan = {"schema_version": 1, "phys": [phy("p", False)], "radio_demands": [radio("a")]}
        self.assertEqual(evaluate(plan)["status"], "infeasible")
        result = evaluate(plan, True)
        self.assertEqual(result["status"], "feasible")
        self.assertTrue(result["radios"]["uses_candidate_resources"])
        self.assertEqual(result["physical_qualification"], "not_evaluated")
        plan["phys"][0]["available"] = False
        self.assertEqual(evaluate(plan, True)["status"], "infeasible")

    def test_dbdc_requires_separate_phys_not_two_band_names(self):
        p = phy("single")
        p["bands"].append("2.4")
        b = radio("b", 2412)
        b["band"] = "2.4"
        plan = {"schema_version": 1, "phys": [p], "radio_demands": [radio("a"), b]}
        self.assertEqual(evaluate(plan)["status"], "infeasible")

    def test_backtracks_when_first_receiver_should_be_reserved(self):
        plan = {"schema_version": 1, "receivers": [window("wide", 100, 105), window("narrow", 100, 101)],
                "receiver_demands": [rx("low", 100.5), rx("high", 104)]}
        plan["receivers"][0]["max_demodulators"] = 1
        result = evaluate(plan)
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["receivers"]["assignment"]["wide"], ["high"])

    def test_entire_signal_must_fit_one_window(self):
        plan = {"schema_version": 1, "receivers": [window("a", 100, 101), window("b", 101, 102)],
                "receiver_demands": [rx("straddle", 101, 0.025)]}
        self.assertEqual(evaluate(plan)["status"], "infeasible")
        plan["receiver_demands"] = [rx("boundary", 100.5, 1)]
        self.assertEqual(evaluate(plan)["status"], "feasible")

    def test_demodulator_count_does_not_extend_rf_coverage(self):
        plan = {"schema_version": 1, "receivers": [window("duo", 851, 852.536)],
                "receiver_demands": [rx("control", 851.5), rx("voice", 854)]}
        self.assertEqual(evaluate(plan)["status"], "infeasible")

    def test_reserved_aircraft_receiver_cannot_be_claimed_by_scanner(self):
        plan = {"schema_version": 1, "receivers": [window("aircraft", 1088, 1092, ["adsb"])],
                "receiver_demands": [rx("scanner", 1090)]}
        self.assertEqual(evaluate(plan)["status"], "infeasible")

    def test_malformed_values_and_duplicate_ids_rejected(self):
        plan = {"schema_version": 1, "phys": [phy("p")], "radio_demands": [radio("a")]}
        for field, value in (("width_mhz", -1), ("center_mhz", float("nan")), ("width_mhz", True)):
            bad = copy.deepcopy(plan)
            bad["radio_demands"][0][field] = value
            with self.assertRaises(ValueError):
                evaluate(bad)
        bad = copy.deepcopy(plan)
        bad["phys"].append(phy("p"))
        with self.assertRaises(ValueError):
            evaluate(bad)
        with self.assertRaises(ValueError):
            evaluate({"schema_version": 1})

    def test_cli_returns_nonzero_for_shortage_and_invalid_input(self):
        script = Path(__file__).resolve().parents[1] / "resources.py"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps({"schema_version": 1, "radio_demands": [radio("a")]}))
            result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["status"], "infeasible")
            path.write_text("{}")
            result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
