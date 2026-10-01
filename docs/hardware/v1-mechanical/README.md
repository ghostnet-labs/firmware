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
| `ltw_rcp-6apffh-scm7001.step` | Amphenol LTW sealed Ethernet receptacle (B-06) | Missing. Request the drawing and STEP from Amphenol LTW |
| `amphenol_10164227-1004a1rlf.step` | CM5 board-to-board connector | Missing. amphenol-cs.com blocks automated downloads |
| `te_2199119-6.step` | M.2 E-key socket | Missing. te.com blocks automated downloads |
| `gw16170.step` | Gateworks GW16170 | Missing. Not published; request from Gateworks support |
| `aiw-170bq.step` | Advantech AIW-170BQ-001 | Missing. Advantech downloads need an account |

Still unselected and therefore not modeled: the Ethernet magnetics, the RF
bulkhead connectors and pigtails, the radio load switches' positions, and the
enclosure and its bosses.

## Current result

See `out/report.md` for the generated version.

- **§18 layout mostly closes.** No part collides with another, the CM5 sits at
  Z 2.89–7.51 mm on its 4.0 mm stacking connector, and no switching part sits
  under or beside an RF module.
- **Shunt is 14.0 mm from the GNSS block.** The §18 rule asks for 15 mm. The
  shunt and INA228 (bottom side, X 70–82) need to move about 1 mm left, or the
  GNSS block right.
- **Corner bosses do not fit.** §18 asks for four M3 points without placing
  them. At an assumed 4 mm corner inset, every corner boss lands on a part: the
  HaLow and Wi-Fi cards at the top corners, the Ethernet keepout lower left and
  the GNSS block lower right. The bosses need other positions or the enclosure
  needs another fixing scheme.
- **Pack contacts.** With the assumed pack-to-board mapping (board centered in
  the 124 × 74 mm pack frame, mirrored in X), the pogo daughterboard zone sits
  below the HaLow socket, the finger end of the HaLow card and the left edge of
  the CM5 (X 12.5–36.5, Y 25.5–41.5). With the other mirror it
  lands below the Wi-Fi socket. No bottom-side part reaches it either way.
- **Occupancy.** Top side 63 %, bottom side 15 %, against §18's 58 % and 16 %.
  The top-side figure includes the 20 × 20 mm Ethernet keepout, which §18
  leaves out.
- **Height.** The CM5 is the tallest top-side part with a known height
  (7.51 mm). The Ethernet receptacle, magnetics and power-input heights are
  unknown until those drawings arrive.
