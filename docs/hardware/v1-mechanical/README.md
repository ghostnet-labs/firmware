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

Still unselected and therefore not modeled exactly: the 8-pin Ethernet header
and pigtail part (modeled as a box, D-025), the Ethernet magnetics, the RF bulkhead
connectors and pigtails, the radio load switches' positions, and the enclosure
and its bosses.

## Current result

See `out/report.md` for the generated version. The check passes with these
changes to the §18 first pass, adopted as decision D-025 (marked "D-025" in
`parts.yaml`):

- **Radio cards move 3 mm toward the CM5** (HaLow X 7–29, Wi-Fi X 88–110,
  sockets follow). That leaves 2 mm to the CM5 and frees both top corners.
- **GNSS moves 2 mm up** (Y 7–21) to clear the lower-right corner.
- **Shunt and INA228 move 2 mm left** (X 68–80, bottom side). That puts them
  16.0 mm from the GNSS block, inside the 15 mm rule.
- **Four M3 fixing points** (6 mm keepout through the full height): top-left,
  top-right and bottom-right corners at a 3.5 mm inset, plus one on the left
  edge at Y 29, just above the Ethernet feed-through.
- **Ethernet path.** The LTW feed-through's inner RJ45 takes a pigtail: an RJ45
  plug and boot (16 × 16 mm around the connector axis, X 20–40.5, 4 mm above
  the PCB) and a thin 8-wire bend down to an 8-pin right-angle header at
  X 41–53, Y 2–7. The board-side RJ45 is retired (R-18). The USB hub and the
  magnetics sit under the plug, so they must stay under 3.5 mm tall.

Other results:

- **CM5 connectors (Amphenol STEP).** Both 10164227-1004A1RLF connectors sit
  under the CM5's own connectors, centered at X 56.0 and Y 27.0 / 61.0. They
  are 3.9 mm tall and mate with the CM5 at 4.0 mm.
- **M.2 cards and sockets (Gateworks and TE STEP).** The socket is 3.0 mm tall
  and holds the card's underside 1.2 mm above the carrier. The GW16170 reaches
  5.99 mm, and its antenna connector overhangs the far end by 0.7 mm.
- **Height.** The feed-through's nut (26.66 mm, connector axis 12 mm above
  the PCB, assumed) sets the enclosure height at the Ethernet wall. Everything
  else on the top side stays under the CM5's 7.51 mm.
- **Pack contacts.** With the assumed pack-to-board mapping, the pogo zone
  sits below the HaLow socket and the left edge of the CM5. No bottom-side
  part reaches it.
- **Occupancy.** Top side 65 %, bottom side 15 %, against §18's 58 % and 16 %.
