# Hardware resource qualification tools (GHO-62)

Python 3.8+ and its standard library are required. Install Python separately on minimal OpenWrt images; this PR does not add it to production firmware. These tools work offline and do not change interfaces, channels, regulators, storage or running services.

The canonical engineering procedures and source evidence are in [the Maer hardware qualification record](https://github.com/ghostnet-labs/docs/blob/main/project/poc/maer-hardware-qualification.md). Requirements remain in the [capability contract](https://github.com/ghostnet-labs/docs/blob/main/project/requirements/maer-capabilities.md). Status and qualification results belong in Linear, not example JSON.

## Collect a node inventory

```sh
python3 scripts/hardware-qualification/inventory.py --output node1-before.json
```

Run on the target itself. Reading some commands (especially `dmesg`) may require root; failed and missing commands remain explicit. The collector uses exclusive creation, so it refuses to overwrite existing evidence. Each command has a ten-second limit and captured output is limited to 256 KiB with a truncation flag. Save a separate complete log where truncated output matters. Sysfs sensor values retain their Linux raw units; the collector does not assert calibration. No register writes or active device probes occur.

The inventory captures board/kernel, PCIe/USB topology, PHY/interface combinations and regulatory state, disks/mounts, module list, kernel log and available thermal/hwmon/frequency readings. Firmware versions, card/carrier revisions, supply/cooling, fixture hashes and actual tests must be recorded separately. Missing thermal/throttling data is unknown, not a pass; CPU frequency alone does not prove absence of throttling. Inventory on an x86 development host is only a tool smoke test.

## Evaluate simultaneous resource demands

```sh
python3 scripts/hardware-qualification/resources.py scripts/hardware-qualification/examples/shortage.json --allow-candidates
python3 scripts/hardware-qualification/resources.py scripts/hardware-qualification/examples/shared-channel.json --allow-candidates
```

The first synthetic example reports both a channel shortage and an uncovered scanner frequency and exits 1. The second changes the model to permitted same-channel sharing and an in-window voice frequency and exits 0. Neither contains a real public-safety frequency plan or qualified hardware. Without `--allow-candidates`, those examples fail because their resources are explicitly unqualified.

`schema_version` is 1. All four arrays are optional, but at least one demand is required:

| Array | Fields |
|---|---|
| `phys` | Unique `id`, explicit `available`/`qualified` booleans, `bands` strings, `combinations` |
| PHY combination | `max_interfaces`, `max_channels`, `limits`: each limit has a unique set of `roles` and a shared `max`; each role appears in only one limit per combination |
| `radio_demands` | Unique `id`, `band`, `role`, positive `center_mhz` and `width_mhz`; one demand consumes one interface |
| `receivers` | Unique `id`, explicit `available`/`qualified`, permitted `services`, fixed `low_mhz`/`high_mhz` capture window and `max_demodulators` |
| `receiver_demands` | Unique `id`, `service`, positive `center_mhz` and occupied `width_mhz` |

Represent DBDC as separate actual PHYs. Listing two bands on one PHY does not make two simultaneous tuning resources. A channel identity includes band, center and width; same-channel AP/mesh sharing must fit one explicitly permitted combination. Unknown/stale/failed resources should be unavailable; do not mark a resource qualified from enumeration. Derive combinations from runtime evidence and verified concurrency, not this example. Receiver windows use actual usable bandwidth after edge guards; sample rate is not usable RF bandwidth. Services reserve aircraft receivers from scanner allocation. All demanded occupied bandwidth must fit one window; the tool does not stitch adjacent receivers or retune them. Multiple channels in a window each consume one modeled demodulator.

Output distinguishes `feasible`, `infeasible`, `indeterminate` (search budget exhausted) and `invalid`. Exit status is 0 only for model feasibility, 1 for shortage/indeterminate, 2 for invalid input. Input is limited to 32 entries per array; allocation searches at most 50,000 states. `physical_qualification` always remains `not_evaluated`. Feasibility covers the supplied model only: it does not validate regulatory legality, firmware behavior, power, RF isolation, USB capacity, timing, CPU, decoding or migration continuity. This is a planning tool, not the runtime GHO-59 controller.

## Inspect installed scanner prerequisites (GHO-66)

Unpack and verify the chosen released SDRtrunk archive separately. For the pinned v0.6.1 runtime, run this **inside the intended target userspace**:

```sh
python3 scripts/hardware-qualification/decoder_preflight.py \
  --installation /opt/sdr-trunk-linux-aarch64-v0.6.1 \
  --sdrplay-library /usr/local/lib/libsdrplay_api.so \
  --output node1-decoder-preflight.json
```

The tool reads metadata only: it never starts Java, loads native libraries, probes receivers or changes services. It checks the bundled Java release metadata against 23.0.1, ELF64 architecture against the current host, declared loader presence/architecture, JVM/API library architecture and launcher executability. Reads are bounded and special files are rejected. Use the exact API path installed in the supported userspace; a filename or matching ELF architecture cannot identify its API version or prove it loads. No SDRplay binary or license is bundled or installed here.

Exit 1 and `prerequisite_status: blocked` identify a missing/mismatched prerequisite. Exit 0 means `prerequisites_present` only; deployment and physical qualification remain `not_evaluated`. The explicit remaining checks include application integrity, transitive native dependencies, API version/license/service, USB access, headless channel lifecycle, audio/stream delivery, replay/live decode and simultaneous workload capacity. Output creation is exclusive. An x86 host inspection is not an ARM64 target result.

The released ARM64 Java loader requires glibc. On a default musl rootfs it can be absent even when the Java binary exists; this preflight makes that case visible. Installing only a loader symlink does not provide a compatible userspace. A Debian/glibc deployment or isolated glibc userspace needs its own library/device/service qualification. Run this script inside that environment so host paths are meaningful. The source audit and archive pin live in the canonical qualification record linked above.

## Run a workload and log throttling/undervoltage (GHO-61)

```sh
python3 scripts/hardware-qualification/workload_run.py run --config node1-full.json --output node1-full-run1.jsonl
python3 scripts/hardware-qualification/workload_run.py mark --output node1-full-run1.jsonl --label swap-start
python3 scripts/hardware-qualification/workload_run.py mark --output node1-full-run1.jsonl --label swap-end
python3 scripts/hardware-qualification/workload_run.py analyze node1-full-run1.jsonl
```

`run` reads a JSON config (see [`examples/workload-smoke.json`](examples/workload-smoke.json), a five-second-per-phase tool smoke test, not a qualification run). Then it:

- hashes every fixture with SHA-256 before creating the log. A missing fixture or a mismatch with an optional expected `sha256` exits 2 and creates nothing.
- records operator board fields (SKU, revisions, cooling, ambient), detected board files (device-tree model/serial/compatible, `/etc/board.json`, OpenWrt/OS release, kernel, boot ID), and output from `uname -a`, `vcgencmd version` and every `version_commands` entry.
- runs `phases` in order. Each phase starts its `commands` (argv lists, no shell unless you call one) in their own process groups, samples for `duration_s`, then stops them with SIGTERM and, after 5 s, SIGKILL. Early exits, spawn failures and return codes are logged.
- writes one sample every `interval_s` (0.1 to 60 s, default 1).

Throttling evidence per sample:

- On a CM5, `vcgencmd get_throttled` is decoded into current and since-boot undervoltage, frequency-cap, throttle and soft-temperature-limit flags. A failed read is flagged, not treated as clean.
- On every target, the sample also records thermal zones (flagged when at or above a `passive`, `hot` or `critical` trip point), nonzero hwmon `*_alarm` files (`in*_lcrit/min_alarm` flagged as undervoltage) and cpufreq current/maximum, which is informational only.

The log is JSON lines with `event`, `seq`, `utc`, per-boot monotonic `mono_s` and the boot ID. It is created exclusively, flushed per line and fsynced by default (`"fsync": false` to reduce flash writes), and a torn last line after power loss is reported, not fatal.

Pack-swap continuity: start a full-load run, then `mark` `swap-start` when the pack is removed and `swap-end` after the replacement is seated. Marks append to the existing log from a second shell. `analyze` takes one or more logs in order (for example the log before and after an unexpected reboot). It reports:

- sampling gaps above `--max-gap-s` (default 2.5 x the configured interval) and boot-ID changes.
- every protection flag with its first time and sample count.
- for each swap window, `continuous`, `interrupted` (reboot, gap, protection flag, unbracketed by samples, or no `swap-end` because the logger died) or `invalid` (shorter than `--swap-min-s`, default 10 s, the owner's minimum).

Exit 0 only when samples exist and nothing was detected. A clean result means no interruption or flag was detected by these sources. It is not physical qualification: whole-node power capture, service-level continuity (voice, decode, recordings, EUD) and radio protection behavior are measured separately per the qualification record.

## Tests

```sh
python3 -m unittest discover -s scripts/hardware-qualification/tests -v
```

CI runs the regression suite for changed tooling on Python 3.8. Scenarios cover workload config validation, fixture hashing, CM5 and sysfs throttling detection, phase/process lifecycle, gap/reboot detection and pack-swap window verdicts, channel shortages, grouped interface limits, cross-band contention, candidate exclusion, full occupied-bandwidth coverage, receiver ownership, malformed input and missing/failed/timed-out inventory commands.
