import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workload_run as wr  # noqa: E402


def write(root, relative, text):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class FakeRunner:
    """Stands in for inventory.command; answers by the joined argv."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return self.answers.get(" ".join(argv), {"argv": argv, "status": "missing"})


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeProcess:
    def __init__(self, argv, exit_after_polls=None):
        self.argv = argv
        self.pid = 4000 + len(argv)
        self.polls = 0
        self.exit_after_polls = exit_after_polls
        self.stopped = False

    def poll(self):
        self.polls += 1
        if self.exit_after_polls is not None and self.polls > self.exit_after_polls:
            return 3
        return None

    def stop(self):
        self.stopped = True
        return 3 if self.exit_after_polls is not None else -15


class StreamLog(wr.Log):
    def __init__(self, clock):
        super().__init__(io.StringIO(), fsync=False, clock=clock)

    def records(self):
        return [json.loads(line) for line in self.stream.getvalue().splitlines()]


def make_config(directory, **overrides):
    config = {"schema_version": 1, "run_id": "t1", "interval_s": 1,
              "phases": [{"name": "load", "duration_s": 3, "commands": [["stress", "--cpu", "4"]]}]}
    config.update(overrides)
    path = Path(directory) / "run.json"
    path.write_text(json.dumps(config))
    return path


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = self.temp.name

    def test_valid_config_gets_defaults(self):
        config = wr.load_config(make_config(self.dir))
        self.assertEqual(config["interval_s"], 1.0)
        self.assertTrue(config["fsync"])
        self.assertEqual(config["fixtures"], [])

    def test_invalid_configs_are_rejected(self):
        cases = {
            "schema": {"schema_version": 2},
            "interval": {"interval_s": 0},
            "interval_bool": {"interval_s": True},
            "no_phases": {"phases": []},
            "duration": {"phases": [{"name": "a", "duration_s": 0}]},
            "duplicate": {"phases": [{"name": "a", "duration_s": 1}, {"name": "a", "duration_s": 1}]},
            "argv": {"phases": [{"name": "a", "duration_s": 1, "commands": ["stress --cpu 4"]}]},
            "empty_argv": {"phases": [{"name": "a", "duration_s": 1, "commands": [[]]}]},
            "fixture_hash": {"fixtures": [{"path": "x", "sha256": "ABC"}]},
            "version_command": {"version_commands": [["ok"], "bad"]},
        }
        for name, override in cases.items():
            with self.subTest(name):
                with self.assertRaises(wr.ConfigError):
                    wr.load_config(make_config(self.dir, **override))

    def test_fixture_hash_recorded_relative_to_config(self):
        write(self.dir, "iq/capture.bin", "abc")
        config = wr.load_config(make_config(self.dir, fixtures=[{"path": "iq/capture.bin"}]))
        records = wr.hash_fixtures(config["fixtures"])
        self.assertEqual(records[0]["sha256"],
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(records[0]["bytes"], 3)

    def test_fixture_mismatch_and_missing_stop_the_run(self):
        write(self.dir, "a.bin", "abc")
        mismatch = wr.load_config(make_config(self.dir, fixtures=[{"path": "a.bin", "sha256": "0" * 64}]))
        with self.assertRaises(wr.ConfigError):
            wr.hash_fixtures(mismatch["fixtures"])
        missing = wr.load_config(make_config(self.dir, fixtures=[{"path": "nope.bin"}]))
        with self.assertRaises(wr.ConfigError):
            wr.hash_fixtures(missing["fixtures"])


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        write(self.root, "proc/sys/kernel/random/boot_id", "boot-a\n")
        write(self.root, "proc/loadavg", "3.10 2.00 1.00 2/100 999\n")

    def test_cm5_throttle_bits_decoded(self):
        runner = FakeRunner({"vcgencmd get_throttled": {"status": "ok", "output": "throttled=0x50005\n"}})
        sample = wr.Probe(self.root, runner).sample()
        self.assertEqual(sample["throttled"]["raw"], "0x50005")
        self.assertEqual(sample["flags"], ["under_voltage_now", "throttled_now",
                                           "under_voltage_occurred", "throttled_occurred"])
        self.assertEqual(sample["boot_id"], "boot-a")
        self.assertEqual(sample["load1"], "3.10")

    def test_clean_cm5_has_no_flags(self):
        runner = FakeRunner({"vcgencmd get_throttled": {"status": "ok", "output": "throttled=0x0\n"}})
        self.assertEqual(wr.Probe(self.root, runner).sample()["flags"], [])

    def test_failed_vcgencmd_is_flagged_not_clean(self):
        runner = FakeRunner({"vcgencmd get_throttled": {"status": "failed", "output": "VCHI init failed"}})
        self.assertEqual(wr.Probe(self.root, runner).sample()["flags"], ["throttle_read_failed"])

    def test_non_cm5_uses_thermal_trips_and_hwmon_alarms(self):
        zone = "sys/class/thermal/thermal_zone0/"
        write(self.root, zone + "type", "cpu-thermal")
        write(self.root, zone + "temp", "86000")
        write(self.root, zone + "trip_point_0_type", "passive")
        write(self.root, zone + "trip_point_0_temp", "85000")
        write(self.root, zone + "trip_point_1_type", "critical")
        write(self.root, zone + "trip_point_1_temp", "105000")
        write(self.root, "sys/class/hwmon/hwmon0/name", "ina228")
        write(self.root, "sys/class/hwmon/hwmon0/in1_lcrit_alarm", "1")
        write(self.root, "sys/class/hwmon/hwmon0/power1_max_alarm", "0")
        write(self.root, "sys/class/hwmon/hwmon0/in1_input", "1")
        write(self.root, "sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq", "1200000")
        sample = wr.Probe(self.root, FakeRunner()).sample()
        self.assertIsNone(sample["throttled"])
        self.assertEqual(sample["flags"], ["thermal_trip:thermal_zone0:passive",
                                           "undervoltage:ina228:in1_lcrit_alarm"])
        self.assertEqual(sample["thermal"][0]["temp_mc"], 86000)
        self.assertEqual(sample["cpufreq"]["policy0"]["cur_khz"], 1200000)

    def test_board_metadata_read_from_root(self):
        write(self.root, "proc/device-tree/model", "Raspberry Pi Compute Module 5\x00")
        board = wr.Probe(self.root, FakeRunner()).board()
        self.assertEqual(board["proc/device-tree/model"], "Raspberry Pi Compute Module 5")
        self.assertIsNone(board["etc/openwrt_release"])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        write(self.temp.name, "proc/sys/kernel/random/boot_id", "boot-a")
        self.clock = FakeClock()
        self.log = StreamLog(self.clock)
        self.spawned = []

    def spawn(self, exit_after_polls=None):
        def factory(argv):
            process = FakeProcess(argv, exit_after_polls)
            self.spawned.append(process)
            return process
        return factory

    def run_config(self, spawn, **overrides):
        config = wr.load_config(make_config(self.temp.name, **overrides))
        runner = FakeRunner({"vcgencmd get_throttled": {"status": "ok", "output": "throttled=0x0"}})
        return wr.run(config, [], self.log, wr.Probe(self.temp.name, runner), spawn=spawn,
                      clock=self.clock, sleep=self.clock.sleep, runner=runner)

    def test_phase_samples_at_interval_and_stops_processes(self):
        self.assertEqual(self.run_config(self.spawn()), "completed")
        records = self.log.records()
        events = [r["event"] for r in records]
        self.assertEqual(events[0], "run_start")
        self.assertEqual(events.count("sample"), 4)  # t = 0, 1, 2, 3 s
        self.assertEqual(events[-1], "run_end")
        self.assertTrue(self.spawned[0].stopped)
        exits = [r for r in records if r["event"] == "process_exit"]
        self.assertEqual(len(exits), 1)
        self.assertFalse(exits[0]["early"])
        start = records[0]
        self.assertEqual(start["physical_qualification"], "not_evaluated")
        self.assertEqual(start["phases"][0]["commands"], [["stress", "--cpu", "4"]])
        self.assertEqual([r["seq"] for r in records], list(range(1, len(records) + 1)))

    def test_early_exit_recorded_once(self):
        self.run_config(self.spawn(exit_after_polls=1))
        exits = [r for r in self.log.records() if r["event"] == "process_exit"]
        self.assertEqual(len(exits), 1)
        self.assertTrue(exits[0]["early"])
        self.assertEqual(exits[0]["returncode"], 3)

    def test_spawn_failure_is_logged_and_run_continues(self):
        def broken(_argv):
            raise FileNotFoundError("stress")
        self.assertEqual(self.run_config(broken), "completed")
        self.assertIn("process_error", [r["event"] for r in self.log.records()])

    def test_interrupt_stops_processes_and_logs_status(self):
        calls = {"n": 0}

        def sleep(seconds):
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            self.clock.sleep(seconds)

        config = wr.load_config(make_config(self.temp.name))
        status = wr.run(config, [], self.log, wr.Probe(self.temp.name, FakeRunner()), spawn=self.spawn(),
                        clock=self.clock, sleep=sleep, runner=FakeRunner())
        self.assertEqual(status, "interrupted")
        self.assertTrue(self.spawned[0].stopped)
        self.assertEqual(self.log.records()[-1], dict(self.log.records()[-1], event="run_end", status="interrupted"))


def sample(mono, utc_s, boot="boot-a", flags=()):
    return {"event": "sample", "mono_s": mono, "utc": "2026-10-09T00:00:%06.3f+00:00" % utc_s,
            "boot_id": boot, "flags": list(flags)}


def mark(label, utc_s):
    return {"event": "mark", "label": label, "utc": "2026-10-09T00:00:%06.3f+00:00" % utc_s}


class AnalyzeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def log(self, records, name="run.jsonl", torn=False):
        path = Path(self.temp.name) / name
        text = "".join(json.dumps(r) + "\n" for r in records)
        path.write_text(text + ('{"event": "sam' if torn else ""))
        return path

    def steady(self, start, end, boot="boot-a", flags_at=None):
        return [sample(t, t, boot, ["under_voltage_now"] if t == flags_at else ())
                for t in range(start, end)]

    def test_continuous_swap_window_passes(self):
        records = self.steady(0, 12) + [mark("swap-start", 12.5)] + self.steady(12, 25)
        records.insert(len(records) - 1, mark("swap-end", 23.5))
        report = wr.analyze([self.log(records)], max_gap_s=2.5)
        window = report["swap_windows"][0]
        self.assertEqual(window["verdict"], "continuous", window)
        self.assertEqual(window["duration_s"], 11.0)
        self.assertEqual(report["verdict"], "no_interruption_detected")

    def test_short_window_is_invalid(self):
        records = self.steady(0, 5) + [mark("swap-start", 5.5), mark("swap-end", 9.5)] + self.steady(5, 15)
        records.sort(key=lambda r: r["utc"])
        report = wr.analyze([self.log(records)], max_gap_s=2.5)
        self.assertEqual(report["swap_windows"][0]["verdict"], "invalid")
        self.assertEqual(report["verdict"], "attention")

    def test_power_loss_reboot_inside_window(self):
        first = self.steady(0, 10) + [mark("swap-start", 10.5)]
        second = [sample(t - 20, t, "boot-b") for t in range(30, 40)] + [mark("swap-end", 35.5)]
        second.sort(key=lambda r: r["utc"])
        report = wr.analyze([self.log(first, "a.jsonl", torn=True), self.log(second, "b.jsonl")], max_gap_s=2.5)
        self.assertEqual(report["gaps"][0]["kind"], "reboot")
        window = report["swap_windows"][0]
        self.assertEqual(window["verdict"], "interrupted")
        self.assertIn("reboot inside window", window["problems"])
        self.assertEqual(report["unreadable_lines"], [{"file": str(Path(self.temp.name) / "a.jsonl"), "line": 12}])

    def test_log_ending_without_end_mark_is_interrupted(self):
        records = self.steady(0, 10) + [mark("swap-start", 10.5)]
        window = wr.analyze([self.log(records)], max_gap_s=2.5)["swap_windows"][0]
        self.assertEqual(window["verdict"], "interrupted")

    def test_gap_and_flags_reported(self):
        records = self.steady(0, 5) + self.steady(9, 14, flags_at=12)
        report = wr.analyze([self.log(records)], max_gap_s=2.5)
        self.assertEqual(report["gaps"], [{"kind": "gap", "seconds": 5, "after_utc": records[4]["utc"],
                                           "before_utc": records[5]["utc"]}])
        self.assertEqual(report["flags"]["under_voltage_now"]["samples"], 1)
        self.assertEqual(report["verdict"], "attention")

    def test_flag_inside_window_interrupts(self):
        records = (self.steady(0, 3) + [mark("swap-start", 3.5)] + self.steady(3, 16, flags_at=8)
                   + [mark("swap-end", 15.5)] + self.steady(16, 18))
        records.sort(key=lambda r: r["utc"])
        window = wr.analyze([self.log(records)], max_gap_s=2.5)["swap_windows"][0]
        self.assertEqual(window["verdict"], "interrupted")
        self.assertEqual(window["flags"], ["under_voltage_now"])


class CommandLineTests(unittest.TestCase):
    def test_run_refuses_bad_fixture_without_creating_log(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory, fixtures=[{"path": "missing.bin"}])
            output = Path(directory) / "out.jsonl"
            self.assertEqual(wr.main(["run", "--config", str(config), "--output", str(output)]), 2)
            self.assertFalse(output.exists())

    def test_mark_requires_existing_log_and_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.jsonl"
            self.assertEqual(wr.main(["mark", "--output", str(output), "--label", "swap-start"]), 2)
            output.write_text("")
            self.assertEqual(wr.main(["mark", "--output", str(output), "--label", "swap-start"]), 0)
            record = json.loads(output.read_text())
            self.assertEqual((record["event"], record["label"]), ("mark", "swap-start"))


if __name__ == "__main__":
    unittest.main()
