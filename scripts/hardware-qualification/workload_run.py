#!/usr/bin/env python3
"""Drive configured workload phases and log throttling/undervoltage evidence (GHO-61).

``run`` starts the commands of each configured phase, samples protection state at a
fixed interval and appends timestamped JSON lines to a new raw log. ``mark`` appends an
operator event (for example the start and end of a pack swap) to that log. ``analyze``
reads one or more logs and reports sampling gaps, reboots and protection flags, and
whether each marked pack-swap window kept the node running for the required time.

Nothing here changes clocks, regulators, thermal limits or protection settings. A run
without detected flags is "not detected", never proof that throttling cannot occur.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import time

from inventory import command

SCHEMA_VERSION = 1
MAX_PHASES = 32
MAX_COMMANDS = 16
MAX_FIXTURES = 64
DEFAULT_INTERVAL_S = 1.0
DEFAULT_SWAP_MIN_S = 10.0
TERMINATE_GRACE_S = 5.0

# vcgencmd get_throttled bits (Raspberry Pi firmware documentation).
THROTTLE_BITS = {
    0: "under_voltage_now",
    1: "arm_freq_capped_now",
    2: "throttled_now",
    3: "soft_temp_limit_now",
    16: "under_voltage_occurred",
    17: "arm_freq_capped_occurred",
    18: "throttled_occurred",
    19: "soft_temp_limit_occurred",
}
# Thermal trip types at which the kernel starts cooling or protecting the system.
PROTECTIVE_TRIPS = ("passive", "hot", "critical")
ALARM_FILE = re.compile(r"^(in|curr|power|temp|fan)\d+_(?:[a-z]+_)?alarm$")
UNDERVOLTAGE_ALARM = re.compile(r"^in\d+_(lcrit|min)_alarm$")
THROTTLED_RE = re.compile(r"throttled=(0x[0-9a-fA-F]+)")


class ConfigError(ValueError):
    """The run configuration is unusable; nothing was started."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _argv(value, where):
    if not isinstance(value, list) or not value or not all(isinstance(a, str) and a for a in value):
        raise ConfigError("%s must be a non-empty list of non-empty strings" % where)
    return value


def load_config(path):
    """Load and validate a run configuration. Relative fixture paths follow the config file."""
    try:
        with open(path) as stream:
            config = json.load(stream)
    except (OSError, ValueError) as error:
        raise ConfigError("cannot read config: %s" % error)
    if not isinstance(config, dict) or config.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError("schema_version must be %d" % SCHEMA_VERSION)
    if not isinstance(config.get("run_id"), str) or not config["run_id"]:
        raise ConfigError("run_id is required")
    interval = config.get("interval_s", DEFAULT_INTERVAL_S)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not 0.1 <= interval <= 60:
        raise ConfigError("interval_s must be between 0.1 and 60")
    config["interval_s"] = float(interval)
    config.setdefault("fsync", True)
    if not isinstance(config["fsync"], bool):
        raise ConfigError("fsync must be a boolean")
    board = config.setdefault("board", {})
    if not isinstance(board, dict) or not all(isinstance(v, (str, int, float)) for v in board.values()):
        raise ConfigError("board must be an object of string or number values")
    base = Path(path).resolve().parent
    fixtures = config.setdefault("fixtures", [])
    if not isinstance(fixtures, list) or len(fixtures) > MAX_FIXTURES:
        raise ConfigError("fixtures must be a list of at most %d entries" % MAX_FIXTURES)
    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, dict) or not isinstance(fixture.get("path"), str):
            raise ConfigError("fixtures[%d].path is required" % index)
        expected = fixture.get("sha256")
        if expected is not None and not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise ConfigError("fixtures[%d].sha256 must be 64 lowercase hex digits" % index)
        fixture["resolved"] = str(base / fixture["path"])
    versions = config.setdefault("version_commands", [])
    if not isinstance(versions, list) or len(versions) > MAX_COMMANDS:
        raise ConfigError("version_commands must be a list of at most %d entries" % MAX_COMMANDS)
    for index, argv in enumerate(versions):
        _argv(argv, "version_commands[%d]" % index)
    phases = config.get("phases")
    if not isinstance(phases, list) or not 1 <= len(phases) <= MAX_PHASES:
        raise ConfigError("phases must be a list of 1 to %d entries" % MAX_PHASES)
    names = set()
    for index, phase in enumerate(phases):
        where = "phases[%d]" % index
        if not isinstance(phase, dict) or not isinstance(phase.get("name"), str) or not phase["name"]:
            raise ConfigError("%s.name is required" % where)
        if phase["name"] in names:
            raise ConfigError("%s.name %r is duplicated" % (where, phase["name"]))
        names.add(phase["name"])
        duration = phase.get("duration_s")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
            raise ConfigError("%s.duration_s must be positive" % where)
        commands = phase.setdefault("commands", [])
        if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
            raise ConfigError("%s.commands must be a list of at most %d entries" % (where, MAX_COMMANDS))
        for number, argv in enumerate(commands):
            _argv(argv, "%s.commands[%d]" % (where, number))
    return config


def hash_fixtures(fixtures):
    """Hash every fixture; any missing file or expected-hash mismatch stops the run."""
    records = []
    for fixture in fixtures:
        try:
            actual = sha256(fixture["resolved"])
            size = os.path.getsize(fixture["resolved"])
        except OSError as error:
            raise ConfigError("fixture %s: %s" % (fixture["path"], error))
        expected = fixture.get("sha256")
        if expected is not None and expected != actual:
            raise ConfigError("fixture %s: sha256 %s does not match expected %s" % (fixture["path"], actual, expected))
        records.append({"path": fixture["path"], "sha256": actual, "bytes": size})
    return records


class Probe:
    """Reads protection state below ``root`` (``/`` on a target, a fake tree in tests)."""

    def __init__(self, root="/", runner=command):
        self.root = Path(root)
        self.runner = runner

    def _read(self, relative):
        try:
            return (self.root / relative).read_text(errors="replace").replace("\x00", "").strip()
        except OSError:
            return None

    def _glob(self, pattern):
        return sorted(self.root.glob(pattern))

    def board(self):
        files = ("proc/device-tree/model", "proc/device-tree/serial-number",
                 "proc/device-tree/compatible", "etc/board.json", "etc/openwrt_release",
                 "etc/os-release", "proc/version", "proc/sys/kernel/random/boot_id")
        return {name: self._read(name) for name in files}

    def boot_id(self):
        return self._read("proc/sys/kernel/random/boot_id")

    def throttled(self):
        """Decode ``vcgencmd get_throttled``; None when the command is absent (non-CM5 targets)."""
        result = self.runner(["vcgencmd", "get_throttled"])
        if result.get("status") == "missing":
            return None
        match = THROTTLED_RE.search(result.get("output") or "")
        if result.get("status") != "ok" or not match:
            return {"status": result.get("status"), "raw": result.get("output"), "flags": ["throttle_read_failed"]}
        value = int(match.group(1), 16)
        flags = [name for bit, name in sorted(THROTTLE_BITS.items()) if value >> bit & 1]
        return {"status": "ok", "raw": match.group(1), "flags": flags}

    def thermal(self):
        zones, flags = [], []
        for zone in self._glob("sys/class/thermal/thermal_zone*"):
            temp = self._read(zone / "temp")
            entry = {"zone": zone.name, "type": self._read(zone / "type"), "temp_mc": _int(temp)}
            for trip in sorted(zone.glob("trip_point_*_type")):
                trip_type = self._read(trip)
                trip_temp = _int(self._read(trip.with_name(trip.name.replace("_type", "_temp"))))
                if (trip_type in PROTECTIVE_TRIPS and trip_temp is not None
                        and entry["temp_mc"] is not None and entry["temp_mc"] >= trip_temp):
                    flags.append("thermal_trip:%s:%s" % (zone.name, trip_type))
            zones.append(entry)
        return zones, flags

    def hwmon(self):
        alarms, flags = [], []
        for device in self._glob("sys/class/hwmon/hwmon*"):
            name = self._read(device / "name") or device.name
            for path in sorted(device.iterdir()):
                if not ALARM_FILE.match(path.name):
                    continue
                value = self._read(path)
                if value not in (None, "0"):
                    alarms.append({"hwmon": device.name, "name": name, "file": path.name, "value": value})
                    kind = "undervoltage" if UNDERVOLTAGE_ALARM.match(path.name) else "hwmon_alarm"
                    flags.append("%s:%s:%s" % (kind, name, path.name))
        return alarms, flags

    def cpufreq(self):
        policies = {}
        for policy in self._glob("sys/devices/system/cpu/cpufreq/policy*"):
            policies[policy.name] = {"cur_khz": _int(self._read(policy / "scaling_cur_freq")),
                                     "max_khz": _int(self._read(policy / "scaling_max_freq"))}
        return policies

    def sample(self):
        throttle = self.throttled()
        zones, thermal_flags = self.thermal()
        alarms, hwmon_flags = self.hwmon()
        flags = list(throttle["flags"]) if throttle else []
        flags.extend(thermal_flags)
        flags.extend(hwmon_flags)
        load = self._read("proc/loadavg")
        return {"boot_id": self.boot_id(), "throttled": throttle, "thermal": zones,
                "hwmon_alarms": alarms, "cpufreq": self.cpufreq(),
                "load1": load.split()[0] if load else None, "flags": flags}


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Process:
    """A workload command in its own session so the whole process group can be stopped."""

    def __init__(self, argv):
        self.argv = argv
        self.popen = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
        self.pid = self.popen.pid

    def poll(self):
        return self.popen.poll()

    def stop(self, grace=TERMINATE_GRACE_S):
        if self.popen.poll() is not None:
            return self.popen.returncode
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.pid, sig)
            except ProcessLookupError:
                break
            try:
                return self.popen.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                continue
        return self.popen.wait()


class Log:
    """Append-only JSON lines; each record is flushed (and optionally fsynced) on write."""

    def __init__(self, stream, fsync=True, clock=time.monotonic):
        self.stream = stream
        self.fsync = fsync
        self.clock = clock
        self.seq = 0

    def write(self, event, **fields):
        self.seq += 1
        record = {"event": event, "seq": self.seq, "utc": utc_now(), "mono_s": round(self.clock(), 3)}
        record.update(fields)
        self.stream.write(json.dumps(record, sort_keys=True) + "\n")
        self.stream.flush()
        if self.fsync:
            os.fsync(self.stream.fileno())


def run(config, fixtures, log, probe, spawn=Process, clock=time.monotonic, sleep=time.sleep, runner=command):
    """Run every phase in order; returns "completed" or "interrupted".

    ``fixtures`` is the output of ``hash_fixtures``, computed before the log is created.
    """
    versions = [runner(argv) for argv in [["uname", "-a"], ["vcgencmd", "version"]] + config["version_commands"]]
    log.write("run_start", schema_version=SCHEMA_VERSION, run_id=config["run_id"],
              operator_board=config["board"], detected_board=probe.board(), machine=platform.machine(),
              fixtures=fixtures, versions=versions, interval_s=config["interval_s"],
              phases=[{"name": p["name"], "duration_s": p["duration_s"], "commands": p["commands"]}
                      for p in config["phases"]],
              physical_qualification="not_evaluated")
    status = "completed"
    try:
        for phase in config["phases"]:
            run_phase(phase, config["interval_s"], log, probe, spawn, clock, sleep)
    except KeyboardInterrupt:
        status = "interrupted"
    log.write("run_end", status=status)
    return status


def run_phase(phase, interval, log, probe, spawn, clock, sleep):
    log.write("phase_start", phase=phase["name"], duration_s=phase["duration_s"])
    processes, reported = [], set()
    try:
        for argv in phase["commands"]:
            try:
                process = spawn(argv)
            except OSError as error:
                log.write("process_error", phase=phase["name"], argv=argv, error=str(error))
                continue
            processes.append(process)
            log.write("process_start", phase=phase["name"], argv=argv, pid=process.pid)
        end = clock() + phase["duration_s"]
        while True:
            log.write("sample", phase=phase["name"], **probe.sample())
            for process in processes:
                code = process.poll()
                if code is not None and process.pid not in reported:
                    reported.add(process.pid)
                    log.write("process_exit", phase=phase["name"], argv=process.argv, pid=process.pid,
                              returncode=code, early=True)
            remaining = end - clock()
            if remaining <= 0:
                break
            sleep(min(interval, remaining))
    finally:
        for process in processes:
            if process.pid in reported:
                continue
            early = process.poll() is not None
            code = process.stop()
            log.write("process_exit", phase=phase["name"], argv=process.argv, pid=process.pid,
                      returncode=code, early=early)
        log.write("phase_end", phase=phase["name"])


def read_records(paths):
    """Yield (path, record) from JSON-line logs; a torn final line after power loss is skipped."""
    for path in paths:
        with open(path) as stream:
            for number, line in enumerate(stream, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    yield path, {"event": "unreadable_line", "line": number}
                    continue
                if isinstance(record, dict):
                    yield path, record


def _parse_utc(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def analyze(paths, max_gap_s, swap_min_s=DEFAULT_SWAP_MIN_S, swap_start="swap-start", swap_end="swap-end"):
    """Report gaps, reboots, protection flags and pack-swap continuity windows."""
    samples, marks, unreadable, ends = [], [], [], []
    for path, record in read_records(paths):
        event = record.get("event")
        if event == "sample":
            samples.append(record)
        elif event == "mark":
            marks.append(record)
        elif event == "unreadable_line":
            unreadable.append({"file": str(path), "line": record["line"]})
        elif event == "run_end":
            ends.append(record.get("status"))
    gaps = []
    for previous, current in zip(samples, samples[1:]):
        if previous.get("boot_id") != current.get("boot_id"):
            gaps.append({"kind": "reboot", "after_utc": previous.get("utc"), "before_utc": current.get("utc")})
            continue
        gap = current.get("mono_s", 0) - previous.get("mono_s", 0)
        if gap > max_gap_s:
            gaps.append({"kind": "gap", "seconds": round(gap, 3), "after_utc": previous.get("utc"),
                         "before_utc": current.get("utc")})
    flagged = {}
    for sample in samples:
        for flag in sample.get("flags") or []:
            flagged.setdefault(flag, {"first_utc": sample.get("utc"), "samples": 0})
            flagged[flag]["samples"] += 1
    windows = [swap_window(start, marks, samples, max_gap_s, swap_min_s, swap_end)
               for start in marks if start.get("label") == swap_start]
    ok = not gaps and not flagged and all(w["verdict"] == "continuous" for w in windows)
    return {"schema_version": SCHEMA_VERSION, "samples": len(samples), "gaps": gaps,
            "flags": flagged, "unreadable_lines": unreadable, "run_end_status": ends,
            "swap_windows": windows, "max_gap_s": max_gap_s, "swap_min_s": swap_min_s,
            "verdict": "no_interruption_detected" if ok and samples else "attention",
            "physical_qualification": "not_evaluated"}


def swap_window(start, marks, samples, max_gap_s, swap_min_s, swap_end):
    begin = _parse_utc(start.get("utc"))
    end_mark = next((m for m in marks if m.get("label") == swap_end
                     and (_parse_utc(m.get("utc")) or 0) >= (begin or 0)), None)
    result = {"start_utc": start.get("utc"), "end_utc": end_mark.get("utc") if end_mark else None}
    if begin is None:
        result.update(verdict="invalid", reason="unreadable start time")
        return result
    if end_mark is None:
        # The logger did not survive to record the end: the node lost power or the run stopped.
        last = samples[-1].get("utc") if samples else None
        result.update(verdict="interrupted", reason="no %s mark after start; last sample %s" % (swap_end, last))
        return result
    finish = _parse_utc(end_mark.get("utc"))
    result["duration_s"] = round(finish - begin, 3)
    inside = [s for s in samples if begin <= (_parse_utc(s.get("utc")) or -1) <= finish]
    before = [s for s in samples if (_parse_utc(s.get("utc")) or -1) < begin]
    if before:
        inside.insert(0, before[-1])
    after = [s for s in samples if (_parse_utc(s.get("utc")) or -1) > finish]
    if after:
        inside.append(after[0])
    problems = []
    if result["duration_s"] < swap_min_s:
        problems.append("window %.3f s shorter than required %.3f s" % (result["duration_s"], swap_min_s))
    if len({s.get("boot_id") for s in inside}) > 1:
        problems.append("reboot inside window")
    for previous, current in zip(inside, inside[1:]):
        if previous.get("boot_id") == current.get("boot_id"):
            gap = current.get("mono_s", 0) - previous.get("mono_s", 0)
            if gap > max_gap_s:
                problems.append("sampling gap %.3f s" % gap)
    if not inside or not before or not after:
        problems.append("window not bracketed by samples")
    flags = sorted({f for s in inside for f in (s.get("flags") or [])})
    result["flags"] = flags
    if flags:
        problems.append("protection flags present")
    if result["duration_s"] < swap_min_s:
        result.update(verdict="invalid", problems=problems)
    else:
        result.update(verdict="interrupted" if problems else "continuous", problems=problems)
    return result


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    run_parser = sub.add_parser("run", help="run the configured phases and write a new JSONL log")
    run_parser.add_argument("--config", required=True, type=Path)
    run_parser.add_argument("--output", required=True, type=Path)
    run_parser.add_argument("--root", default="/", help="filesystem root for sysfs/procfs reads")
    mark_parser = sub.add_parser("mark", help="append an operator event to an existing log")
    mark_parser.add_argument("--output", required=True, type=Path)
    mark_parser.add_argument("--label", required=True)
    mark_parser.add_argument("--note", default="")
    analyze_parser = sub.add_parser("analyze", help="summarize one or more logs as JSON")
    analyze_parser.add_argument("logs", nargs="+", type=Path)
    analyze_parser.add_argument("--max-gap-s", type=float, default=None,
                                help="largest allowed spacing between samples (default 2.5 x interval)")
    analyze_parser.add_argument("--swap-min-s", type=float, default=DEFAULT_SWAP_MIN_S)
    args = parser.parse_args(argv)

    if args.action == "run":
        try:
            config = load_config(args.config)
            fixtures = hash_fixtures(config["fixtures"])  # No log is created for unusable fixtures.
        except ConfigError as error:
            print("config error: %s" % error, file=sys.stderr)
            return 2
        # Treat SIGTERM like Ctrl-C so workload processes are always stopped and run_end is logged.
        signal.signal(signal.SIGTERM, _interrupt)
        # Exclusive create protects earlier evidence from accidental replacement.
        with args.output.open("x") as stream:
            status = run(config, fixtures, Log(stream, fsync=config["fsync"]), Probe(args.root))
        return 0 if status == "completed" else 1
    if args.action == "mark":
        if not args.output.is_file():
            print("no such log: %s" % args.output, file=sys.stderr)
            return 2
        with args.output.open("a") as stream:
            Log(stream).write("mark", label=args.label, note=args.note,
                              boot_id=Probe().boot_id())
        return 0
    max_gap = args.max_gap_s
    if max_gap is None:
        intervals = [r.get("interval_s") for _, r in read_records(args.logs) if r.get("event") == "run_start"]
        max_gap = 2.5 * max([i for i in intervals if isinstance(i, (int, float))] or [DEFAULT_INTERVAL_S])
    report = analyze(args.logs, max_gap, args.swap_min_s)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["verdict"] == "no_interruption_detected" else 1


if __name__ == "__main__":
    sys.exit(main())
