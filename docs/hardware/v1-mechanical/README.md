# V1 carrier floorplan model (GHO-7)

Linear: [GHO-7 Assemble manufacturer CAD and verify the V1 carrier floorplan](https://linear.app/ghostnet-labs/issue/GHO-7/assemble-manufacturer-cad-and-verify-the-v1-carrier-floorplan)

A scripted 3D floorplan of the V1 carrier with a clearance checker. It places
every part from `parts.yaml` on the 138 × 67 mm board, using manufacturer STEP
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

Origin at the PCB's lower-left corner. X runs along the 138 mm edge, Y along
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
| `aiw-170bq.step` | Advantech AIW-170BQ-001 | No longer used: V1 Wi-Fi moved to the AsiaRF AW7916-AED (GHO-37), which has no published STEP |

Still unselected and therefore not modeled exactly: the Ethernet magnetics, the RF bulkhead
connectors and pigtails, the radio load switches' positions, and the enclosure
and its bosses.

## Board grows to 138 × 67 mm (GHO-37)

The AIW-170BQ can't run an 802.11s mesh point, and Wi-Fi mesh backhaul is a
V1 requirement, so V1 moves to the AsiaRF AW7916-AED (MT7916). It is M.2
3052, 30 × 52 mm, too wide for the 28 mm beside the CM5. Justin chose to grow
the board rather than put the card underneath (2026-10-02). The board is now
138 × 67 mm:

- **Wi-Fi card** X 88–118, Y 10.9–62.9, antenna end toward the top wall, with
  its socket at Y 7.0–15.7.
- **GNSS** gets its own column at the right edge, X 120.5–136.5, Y 7–21.
- **Right-hand bosses** follow the new edge (X 131.5).

A 120 × 81 mm board (GNSS above the card) also passed; Justin picked the
longer 138 × 67 mm shape (2026-10-02). The matching pack frame is now
145 × 74 mm, with the carrier pogo zone preserved; the enclosure must still
grow to match. `parts.yaml` and the generated files under `out/` are the
canonical current coordinates.

## Current result

See `out/report.md` for the generated version. The check passes. The current
coordinates come from `parts.yaml`; the list below records the adopted D-025
placement rules as updated for the 138 × 67 mm outline:

- **Radio columns.** HaLow stays at X 7–29. The AW7916-AED occupies
  X 88–118, Y 10.9–62.9, with its socket centered at X 92.05–113.95 and
  Y 7.0–15.7. The Wi-Fi card keeps 2 mm clearance from the CM5.
- **GNSS moves to the right-edge column** (X 120.5–136.5, Y 7–21), clear
  of the enlarged Wi-Fi card and the lower-right boss.
- **Shunt and INA228 move 2 mm left** (X 68–80, bottom side). That puts them
  16.0 mm from the GNSS block, inside the 15 mm rule.
- **Four M3 fixing points** (6 mm keepout through the full height): top-left,
  top-right and bottom-right corners at a 3.5 mm inset, plus one on the left
  edge at Y 29, just above the Ethernet feed-through. The right-hand points
  follow the new board edge at X 131.5.
- **Ethernet path.** The LTW feed-through's inner RJ45 takes a pigtail: an RJ45
  plug and boot (16 × 16 mm around the connector axis, X 20–40.5, 4 mm above
  the PCB) and a thin 8-wire bend down to a Molex Pico-Lock 1.50 mm 8-pin
  right-angle header (504050-0891, mated with housing 504051-0801; GHO-45) at
  X 41–54.5, Y 2–9.5, 2.0 mm tall. Its depth is assumed until Molex sales
  drawing SD-504050-001 is checked; it must stay 0.5 mm clear of the USB hub
  at Y 10. The board-side RJ45 is retired (R-18). The USB hub and the
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
- **Occupancy.** Top side 65 %, bottom side 13 %. These are generated from
  the current 138 × 67 mm model.
