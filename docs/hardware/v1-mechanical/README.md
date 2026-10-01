# V1 carrier floorplan model (GHO-7)

Linear: [GHO-7 Assemble manufacturer CAD and verify the 117 × 67 mm carrier floorplan](https://linear.app/ghostnet-labs/issue/GHO-7/assemble-manufacturer-cad-and-verify-the-117-67-mm-carrier-floorplan)

A scripted 3D floorplan of the V1 carrier with a clearance checker. It
places every part from `parts.yaml` on the 117 × 67 mm board, using
manufacturer STEP models from `step/` where present and the envelopes stated in
[`OpenMANET_V1_Current_Project_State.md`](../../OpenMANET_V1_Current_Project_State.md)
otherwise, then reports collisions, wall clearance, CM5 underside keepout
violations and the space left for parts that are still blocked.

This is not the final mechanical CAD. Until the STEP files below are in
`step/`, the output is an envelope study: it can prove that something does
**not** fit, but it cannot freeze coordinates.

## Run

```sh
pip install cadquery pyyaml
python3 floorplan.py        # exits 1 if any FAIL finding
```

Outputs land in `out/`:

| File | Contents |
|------|----------|
| `report.md` | Findings, band space, blocked parts, open assumptions |
| `coordinates.csv` | Placement table (min/max X, Y, Z per part, geometry source) |
| `floorplan.svg` | Top view |
| `assembly.step` | Board + placed parts, for import into any MCAD tool |

## Frame

Origin at the PCB's lower-left corner. X runs along the 117 mm edge, Y along
the 67 mm edge with Y up (so "upper" in the handoff is high Y), Z up from the
PCB top surface. This matches the §29 CM5 working envelope (X 31–86, Y 24–64)
and its hole references.

## STEP models needed

Drop each file into `step/` with the name shown, then set `step_offset` /
`step_rot` in `parts.yaml` so the model lands on the intended footprint and
rerun. Most vendors require a free account to download.

| File | Part | Where |
|------|------|-------|
| `cm5.step` | Raspberry Pi CM5 | datasheets.raspberrypi.com (CM5 mechanical / STEP) |
| `amphenol_10164227-1004a1rlf.step` | CM5 board-to-board connector | amphenol-cs.com product page |
| `te_2199119-6.step` | M.2 E-key socket | te.com product page, CAD tab |
| `gw16170.step` | Gateworks GW16170 | Gateworks support / trac wiki for GW16170 |
| `aiw-170bq.step` | Advantech AIW-170BQ-001 | Advantech product page, downloads |
| `bel_1840888-4.step` | Bel/TRP RJ45 with magnetics | belfuse.com product page |
| `milcon_mc327-5.step` | Mil-Con MC327-5 battery connector | mil-coninc.com resources (3D models) |

Still unselected and therefore not modeled: RF bulkhead connectors and
pigtails (HaLow MMCX, 2 × Wi-Fi MHF4, GNSS), the enclosure and its bosses.

## Current result (envelopes only)

See `out/report.md` for the generated version.

- The CM5 at Y 24–64 leaves a 22.5 mm band below it (after the 1.5 mm wall
  clearance) and only 1.5 mm above it. Everything in the §28 concept that sits
  "below" the CM5 (GNSS, power, USB hub, RJ45, MC327-5) has to fit in that
  22.5 mm band.
- The §29 power region (~45 × 25 mm) does not fit that band: it collides with
  the CM5 and its underside keepout by 2.5 mm. The model uses 45 × 22.5 mm
  (10% less area) until GHO-10 sizes the real power stage.
- Both M.2 2230 cards fit in the 31 mm side bands beside the CM5 with the socket
  at Y 24 and the card tip toward the upper RF wall, with 1 mm to the CM5 and
  0.5 mm to the assumed corner bosses. This credits no card-to-socket overlap,
  so the socket STEP can only make it looser.
- After the CM5, power region, GNSS and USB hub, the lower band has two usable
  slots for the edge connectors: X 17.7–36.0 (18.3 mm) on the left for the
  MC327-5 and X 93.0–109.5 (16.5 mm) on the right for the RJ45, each 22.5 mm
  deep. Whether the MC327-5 and Bel 1840888-4 fit those slots is the open
  question that decides whether 117 × 67 mm holds.
