# V1 carrier floorplan model (GHO-7)

Linear: [GHO-7 Assemble manufacturer CAD and verify the 117 × 67 mm carrier floorplan](https://linear.app/ghostnet-labs/issue/GHO-7/assemble-manufacturer-cad-and-verify-the-117-67-mm-carrier-floorplan)

A scripted 3D floorplan of the V1 carrier with a clearance checker. It
places every part from `parts.yaml` on the 117 × 67 mm board, using
manufacturer STEP models from `step/` where present and the envelopes stated in
the V1 engineering reference ([`project/hardware/v1-reference.md`](https://github.com/ghostnet-labs/docs/blob/main/project/hardware/v1-reference.md) in ghostnet-labs/docs)
otherwise, then reports collisions, wall clearance, CM5 underside keepout
violations and the space left for parts that are still blocked.

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
| `report.md` | Findings, band space, height, blocked parts, open assumptions |
| `coordinates.csv` | Placement table (min/max X, Y, Z per part, geometry source) |
| `floorplan.svg` | Top view |
| `assembly.step` | Board + placed parts for any MCAD tool (not committed, ~160 MB with the CM5 model) |

## Frame

Origin at the PCB's lower-left corner. X runs along the 117 mm edge, Y along
the 67 mm edge with Y up (so "upper" in the handoff is high Y), Z up from the
PCB top surface. This matches the §29 CM5 working envelope (X 31–86, Y 24–64)
and its hole references.

## Manufacturer CAD

Set `step_offset` / `step_rot` in `parts.yaml` so each model lands on its
intended footprint, then rerun.

| File in `step/` | Part | Status |
|------|------|--------|
| `cm5.step` | Raspberry Pi CM5 | In use. Official STEP from the Raspberry Pi Product Information Portal (`fetch_models.sh`) |
| `bel_1840888-4.step` | Bel/TRP RJ45 with magnetics | Missing. Modeled from the Bel customer drawing (`fetch_models.sh`); STEP needs a Bel account |
| `amphenol_10164227-1004a1rlf.step` | CM5 board-to-board connector | Missing. amphenol-cs.com blocks automated downloads |
| `te_2199119-6.step` | M.2 E-key socket | Missing. te.com blocks automated downloads |
| `gw16170.step` | Gateworks GW16170 | Missing. Not published; request from Gateworks support |
| `aiw-170bq.step` | Advantech AIW-170BQ-001 | Missing. Advantech downloads need an account |
| `milcon_mc327-5.step` | Mil-Con MC327-5 battery connector | Missing. mil-coninc.com "3d Model" button asks for contact details |

Still unselected and therefore not modeled: RF bulkhead connectors and
pigtails (HaLow MMCX, 2 × Wi-Fi MHF4, GNSS), the enclosure and its bosses.

## Current result

See `out/report.md` for the generated version.

- **CM5 (official STEP).** It sits at Z 2.89–7.51 mm on the 4.0 mm stacking
  connector, which matches the datasheet's 7.44 mm mounted height.
- **Power region.** The §29 region (~45 × 25 mm) does not fit the 22.5 mm band
  below the CM5. It is modeled at 45 × 22.5 mm until GHO-10 sizes the power
  stage.
- **Radio cards.** Both M.2 2230 cards fit the side bands, with 1 mm to the CM5
  and 0.5 mm to the assumed corner bosses.
- **RJ45 (Bel drawing).** It is 18.67 mm wide including its shield tabs, 21.65
  mm deep and 13.75 mm tall. That does not fit the 16.5 mm lower-right slot left
  by the first pass. It fits once the TUSB4020BI moves to the bottom side, with
  0.5 mm to the corner boss. The hub is kept out of the RJ45's through-hole lead
  area.
- **Enclosure height.** The RJ45 is the tallest top-side part at 13.75 mm, so it
  sets the minimum inner height above the PCB.
- **Battery connector.** An 18.3 × 22.5 mm slot is left for the MC327-5 on the
  lower left, next to the GNSS. Whether it fits there is the open question that
  decides whether 117 × 67 mm holds.
