# V1 carrier floorplan model (GHO-7)

Linear: [GHO-7 Assemble manufacturer CAD and verify the 117 × 67 mm carrier floorplan](https://linear.app/ghostnet-labs/issue/GHO-7/assemble-manufacturer-cad-and-verify-the-117-67-mm-carrier-floorplan)

A scripted 3D floorplan of the V1 carrier with a clearance checker. It places
every part from `parts.yaml` on the 117 × 67 mm board, using manufacturer STEP
models from `step/` where present and the first-pass layout in
[`project/hardware/v1-reference.md`](https://github.com/ghostnet-labs/docs/blob/main/project/hardware/v1-reference.md)
§18 otherwise. The battery contact zone comes from
[`project/hardware/v1-battery-pack.md`](https://github.com/ghostnet-labs/docs/blob/main/project/hardware/v1-battery-pack.md)
(M-10, M-14). It then reports collisions, wall clearance, keepout violations,
the §18 rules (no switching part over or under an RF module, power parts at
least 15 mm from the GNSS receiver), occupancy and GNSS distances.

This is not the final mechanical CAD. Until the STEP files below are in
`step/`, the output is an envelope study: it can prove that something does
**not** fit, but it cannot freeze coordinates.

## Run

```sh
pip install cadquery pyyaml
./fetch_models.sh           # public manufacturer CAD into step/
python3 floorplan.py        # exits 1 if any FAIL finding
```

Vendor CAD files are not committed (`step/` is git-ignored). Copy any you
download by hand into `step/` using the names below. The Manet project keeps
the current set, plus the last generated `assembly.step`, in the project files
under `v1-cad/`.

Outputs land in `out/`:

| File | Contents |
|------|----------|
| `report.md` | Findings, occupancy, GNSS distances, height, blocked parts, open assumptions |
| `coordinates.csv` | Placement table (min/max X, Y, Z per part, geometry source) |
| `floorplan.svg` | Top view |
| `assembly.step` | Board + placed parts for any MCAD tool (not committed, ~160 MB with the CM5 model) |

## Frame

Origin at the PCB's lower-left corner. X runs along the 117 mm edge, Y along
the 67 mm edge with Y up, Z up from the PCB top surface. Bottom-side parts
have negative Z. This matches the §18 coordinates (CM5 at X 31–86, Y 24–64).

## Manufacturer CAD

Set `step_offset` / `step_rot` in `parts.yaml` so each model lands on its
intended footprint, then rerun.

| File in `step/` | Part | Status |
|------|------|--------|
| `cm5.step` | Raspberry Pi CM5 | In use. Official STEP from the Raspberry Pi Product Information Portal (`fetch_models.sh`) |
| `ltw_rcp-5spffh-scu7001.step` | Amphenol LTW RCP-5SPFFH-SCU7001 sealed Ethernet feed-through (D-022) | In use. LTW's equivalent model for the family (`rcp-5spffh-scm7001_eq_asm.stp`); amphenolltw.com refuses scripted downloads, so copy it by hand |
| `amphenol_10164227-1004a1rlf.step` | Amphenol 10164227-1004A1RLF CM5 board-to-board connector (4.0 mm stack) | In use. Supplied by Justin |
| `te_2199119-6.step` | TE 2199119-6 M.2 E-key socket | In use. TE `c-2199119-6-c-3d.stp`, supplied by Justin |
| `gw16170.step` | Gateworks GW16170 | In use. Gateworks `GW16170A.STEP` (surface export; checked with one box per shell), supplied by Justin; not public |
| `aiw-170bq.step` | Advantech AIW-170BQ-001 | Missing. Advantech downloads need an account |

Still unselected and therefore not modeled: the board-side RJ45 jack and the
patch cable to the feed-through, the Ethernet magnetics, the RF bulkhead
connectors and pigtails, the radio load switches' positions, and the enclosure
and its bosses.

## Current result

See `out/report.md` for the generated version.

- **Ethernet does not fit the §18 zone.** The chosen LTW feed-through (D-022)
  mounts in the left wall and has an RJ45 socket on the inside, so a patch
  cable has to run from it to an RJ45 jack on the carrier. Its inner body
  reaches X 20 and stands about 22 mm tall. With the plug and the cable bend
  (assumed to reach 42 mm from the wall in a 25 × 25 mm cross-section), it runs
  into the CM5 edge, the HaLow socket and the USB hub. §18 reserved
  20 × 20 mm.
- **Enclosure height at the Ethernet wall.** With the connector axis 12 mm
  above the PCB (assumed), the feed-through's nut reaches 26.66 mm. That is
  the tallest part by far; everything else on the top side stays under
  7.51 mm (the CM5).
- **Shunt is 14.0 mm from the GNSS block.** The §18 rule asks for 15 mm. The
  shunt and INA228 (bottom side, X 70–82) need to move about 1 mm left, or the
  GNSS block right.
- **Corner bosses do not fit.** §18 asks for four M3 points without placing
  them. At an assumed 4 mm corner inset, every corner boss lands on a part: the
  HaLow and Wi-Fi cards at the top corners, the Ethernet feed-through lower
  left and the GNSS block lower right. The bosses need other positions or the
  enclosure needs another fixing scheme.
- **M.2 cards and sockets (Gateworks and TE STEP).** The TE socket is 3.0 mm
  tall and holds the card centered 1.6 mm above the carrier, so the card's
  underside sits at 1.2 mm. The GW16170 then reaches 5.99 mm, 2.5 mm above
  §18's 3.5 mm card height, and its edge antenna connector overhangs the far
  end by 0.7 mm (to Y 64.7). With the card edge at §18's Y 34, each socket
  spans Y 30.1–38.8.
- **CM5 connectors (Amphenol STEP).** Both 10164227-1004A1RLF connectors sit
  under the CM5's own connectors, centered at X 56.0 and Y 27.0 / 61.0. They
  are 3.9 mm tall and mate with the CM5 at 4.0 mm, which leaves 2.89 mm
  between the carrier and the CM5's underside parts.
- **Pack contacts.** With the assumed pack-to-board mapping (board centered in
  the 124 × 74 mm pack frame, mirrored in X), the pogo daughterboard zone sits
  below the HaLow socket, the finger end of the HaLow card and the left edge of
  the CM5 (X 12.5–36.5, Y 25.5–41.5). With the other mirror it lands below the
  Wi-Fi socket. No bottom-side part reaches it either way.
- **Occupancy.** Top side 67 %, bottom side 15 %, against §18's 58 % and 16 %.
  The top-side figure includes the Ethernet feed-through and plug space, which
  §18 leaves out, and does not yet include the board-side RJ45.
