#!/usr/bin/env python3
"""Read-only installed decoder prerequisites; never loads or executes native code."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import stat
import struct

EXPECTED_JAVA = "23.0.1"
MACHINES = {"aarch64": 183, "arm64": 183, "x86_64": 62, "amd64": 62}


def open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("not a regular file")
    return os.fdopen(fd, "rb")


def elf(path):
    """Read bounded ELF64 metadata, rejecting malformed headers/interpreters."""
    with open_regular(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise ValueError("expected little-endian ELF64")
        values = struct.unpack("<16sHHIQQQIHHHHHH", header)
        _, kind, machine, version, _, offset, _, _, ehsize, entrysize, count, _, _, _ = values
        if version != 1 or ehsize != 64 or kind not in (2, 3):
            raise ValueError("unsupported ELF header")
        if not 0 < count <= 128 or entrysize != 56 or offset < 64 or offset + count * entrysize > size:
            raise ValueError("invalid program header table")
        interpreter = None
        for index in range(count):
            stream.seek(offset + index * entrysize)
            segment = struct.unpack("<IIQQQQQQ", stream.read(entrysize))
            if segment[0] != 3:  # PT_INTERP
                continue
            start, length = segment[2], segment[5]
            if interpreter is not None or not 2 <= length <= 4096 or start + length > size:
                raise ValueError("invalid ELF interpreter segment")
            stream.seek(start)
            raw = stream.read(length)
            if raw[-1:] != b"\0" or b"\0" in raw[:-1]:
                raise ValueError("invalid interpreter string")
            interpreter = raw[:-1].decode("utf-8")
            if not interpreter.startswith("/") or ".." in Path(interpreter).parts:
                raise ValueError("interpreter must be an absolute canonical path")
        return {"machine_id": machine, "elf_class": 64, "elf_type": kind, "interpreter": interpreter}


def regular_text(path):
    with open_regular(path) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular file")
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError("metadata exceeds 64 KiB")
    return data.decode("utf-8")


def inspect(installation, api_library, machine=None):
    installation, api_library = Path(installation), Path(api_library)
    machine = platform.machine() if machine is None else machine
    expected_machine = MACHINES.get(machine.lower())
    checks = []

    def check(name, passed, evidence):
        checks.append({"name": name, "status": "present" if passed else "blocked", "evidence": evidence})

    def binary(name, path, executable=False, shared=False):
        try:
            data = elf(path)
            check(name, expected_machine is not None and data["machine_id"] == expected_machine
                  and (not shared or data["elf_type"] == 3),
                  {"path": str(path), **data})
            if executable:
                check(name + "_executable", os.access(path, os.X_OK), str(path))
            return data
        except (OSError, ValueError, UnicodeError, struct.error) as error:
            check(name, False, {"path": str(path), "error": str(error)})
            return None

    check("host_architecture", expected_machine is not None, machine)
    try:
        release = regular_text(installation / "release")
        versions = re.findall(r'^JAVA_VERSION="([^"\n]+)"$', release, re.MULTILINE)
        check("bundled_java_version", versions == [EXPECTED_JAVA], {"expected": EXPECTED_JAVA, "observed": versions})
    except (OSError, ValueError, UnicodeError) as error:
        check("bundled_java_version", False, str(error))
    java = binary("bundled_java", installation / "bin/java", executable=True)
    if java:
        loader = java["interpreter"]
        check("dynamic_loader_declared", loader is not None, loader)
        if loader:
            binary("dynamic_loader", Path(loader), executable=True)
    binary("bundled_jvm", installation / "lib/server/libjvm.so", shared=True)
    binary("sdrplay_library", api_library, shared=True)
    launcher = installation / "bin/sdr-trunk"
    check("launcher_executable", launcher.is_file() and os.access(launcher, os.X_OK), str(launcher))
    blocked = any(item["status"] == "blocked" for item in checks)
    return {"schema_version": 1, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "machine": machine, "installation": str(installation),
            "prerequisite_status": "blocked" if blocked else "prerequisites_present",
            "checks": checks, "deployment_qualification": "not_evaluated",
            "physical_qualification": "not_evaluated",
            "not_evaluated": ["application_release_integrity", "transitive_native_dependencies",
                              "sdrplay_api_version_and_license", "sdrplay_service_and_usb_access",
                              "headless_channel_lifecycle", "audio_or_stream_delivery",
                              "iq_replay_and_live_decode", "simultaneous_workload_capacity"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True, help="unpacked sdr-trunk release directory")
    parser.add_argument("--sdrplay-library", type=Path, default=Path("/usr/local/lib/libsdrplay_api.so"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Refuse overwriting evidence before inspecting anything.
    with args.output.open("x") as output:
        result = inspect(args.installation, args.sdrplay_library)
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    return 1 if result["prerequisite_status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
