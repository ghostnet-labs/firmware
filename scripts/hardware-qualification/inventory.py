#!/usr/bin/env python3
"""Read-only Linux inventory. Writes only its JSON output; starts no workloads."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile


def command(argv, timeout=10, limit=256 * 1024):
    if not shutil.which(argv[0]):
        return {"argv": argv, "status": "missing"}
    # Bound captured memory, and keep nonzero output rather than interpreting absence as success.
    with tempfile.TemporaryFile() as output:
        try:
            proc = subprocess.run(argv, stdout=output, stderr=subprocess.STDOUT,
                                  timeout=timeout, check=False)
            status = "ok" if proc.returncode == 0 else "failed"
            code = proc.returncode
        except subprocess.TimeoutExpired:
            status, code = "timeout", None
        except OSError as error:
            return {"argv": argv, "status": "failed", "error": str(error)}
        size = output.tell()
        output.seek(0)
        return {"argv": argv, "status": status, "returncode": code,
                "output": output.read(limit).decode("utf-8", errors="replace"),
                "truncated": size > limit}


def read(path):
    try:
        return {"status": "ok", "value": Path(path).read_text(errors="replace").replace("\x00", "").strip()}
    except OSError as error:
        return {"status": "unavailable", "error": str(error)}


def collect():
    paths = ["/proc/device-tree/model", "/proc/cmdline", "/proc/meminfo", "/proc/stat",
             "/etc/os-release", "/etc/openwrt_release", "/proc/diskstats", "/proc/net/dev"]
    for pattern in ("/sys/class/thermal/thermal_zone*/type", "/sys/class/thermal/thermal_zone*/temp",
                    "/sys/class/thermal/thermal_zone*/trip_point_*_type",
                    "/sys/class/thermal/thermal_zone*/trip_point_*_temp",
                    "/sys/class/hwmon/hwmon*/name", "/sys/class/hwmon/hwmon*/temp*_input",
                    "/sys/class/hwmon/hwmon*/in*_input", "/sys/class/hwmon/hwmon*/curr*_input",
                    "/sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq",
                    "/sys/devices/system/cpu/cpufreq/policy*/scaling_governor"):
        paths.extend(str(p) for p in sorted(Path("/").glob(pattern.lstrip("/"))))
    commands = [["uname", "-a"], ["lspci", "-nnk"], ["lsusb"], ["lsusb", "-t"],
                ["iw", "phy"], ["iw", "dev"], ["iw", "reg", "get"], ["ip", "-details", "link"],
                ["lsblk", "-J", "-o", "NAME,TYPE,SIZE,TRAN,MODEL,MOUNTPOINT"],
                ["findmnt", "-J"], ["lsmod"], ["dmesg"], ["vcgencmd", "get_throttled"]]
    return {"schema_version": 1, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "machine": platform.machine(), "physical_qualification": "not_evaluated",
            "files": {path: read(path) for path in paths},
            "commands": [command(argv) for argv in commands]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Exclusive create protects prior evidence from accidental replacement.
    with args.output.open("x") as output:
        json.dump(collect(), output, indent=2, sort_keys=True)
        output.write("\n")


if __name__ == "__main__":
    main()
