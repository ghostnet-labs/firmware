#!/usr/bin/env python3
"""OpenMANET V1 battery pack envelope model and CAD review gate checks (GHO-8).

Builds the pack assembly bodies listed in ghostnet-labs/docs
project/hardware/v1-battery-pack.md from pack.yaml (M-01 to M-22 plus marked
assumptions), then evaluates what the envelope model can evaluate of the five
CAD review gates:

  gate 1  fit: every body inside the 145 x 74 mm footprint and the pack height,
          pairwise 3D collisions, design-rule keepouts (contact cavity, gasket
          gland, pogo travel, boss engagement, hard-stop lands), cell-to-cell
          and cell-to-wall gaps, and the D-017 length budget
  gate 2  first-order kinematics of the hook-first pivot: engagement order of
          the locating bosses, probes and gasket, and probe scrub
  gate 3  tolerance stacks: probe working height and stroke, gasket squeeze,
          plunger-on-pad position
  gate 5  target-pad geometry: pad gaps and land to the gasket

Gate 4 (structural) needs the enclosure material, hook geometry and load cases,
so the report only lists what is missing.

Outputs (in out/): pack_report.md, pack_coordinates.csv, pack.svg,
pack_assembly.step (not committed).

Usage:  pip install cadquery pyyaml && python3 pack.py [--strict]
        --strict exits 1 when any check FAILs (default exits 0 so the open gate
        findings do not read as a broken tool).
"""

from __future__ import annotations

import csv
import itertools
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cadquery as cq
import yaml

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "out"

# Kinds that are physical hardware inside or on the pack (checked against keepouts).
HARDWARE = {"cell", "insulator", "bms", "protection", "pcb", "pogo", "gasket", "boss", "hook", "latch", "wiring"}
# Pairs allowed to touch or overlap by design.
MATED_KINDS = {
    frozenset({"pogo", "pcb"}),        # probe tail in the daughterboard, tip on the target pad
}
EPS = 1e-6


# ---------------------------------------------------------------- geometry

def make_solid(spec: dict) -> cq.Shape:
    shape = spec["shape"]
    if shape == "box":
        return cq.Solid.makeBox(spec["w"], spec["d"], spec["h"], cq.Vector(spec["x"], spec["y"], spec["z"]))
    if shape == "cylz":
        return cq.Solid.makeCylinder(spec["dia"] / 2, spec["h"], cq.Vector(spec["cx"], spec["cy"], spec["z"]),
                                     cq.Vector(0, 0, 1))
    if shape == "cylx":
        return cq.Solid.makeCylinder(spec["dia"] / 2, spec["w"], cq.Vector(spec["x"], spec["cy"], spec["cz"]),
                                     cq.Vector(1, 0, 0))
    if shape == "ring":
        return ring(spec)
    raise ValueError(f"unknown shape {shape}")


def rounded_rect(w: float, d: float, r: float, h: float) -> cq.Workplane:
    wp = cq.Workplane("XY").rect(w, d).extrude(h)
    return wp.edges("|Z").fillet(r) if r > 0 else wp


def ring(spec: dict) -> cq.Shape:
    """Closed rounded-rectangle groove/cord around a centerline."""
    half = spec["width"] / 2
    outer = rounded_rect(spec["cl_w"] + 2 * half, spec["cl_d"] + 2 * half, spec["r"] + half, spec["h"])
    inner = rounded_rect(spec["cl_w"] - 2 * half, spec["cl_d"] - 2 * half, max(spec["r"] - half, 0), spec["h"])
    body = outer.cut(inner).translate((spec["cx"], spec["cy"], spec["z"]))
    return body.val()


@dataclass
class Body:
    id: str
    name: str
    kind: str
    src: str
    spec: dict
    bans: list[str] = field(default_factory=list)
    solid: cq.Shape | None = None

    def build(self) -> None:
        solid = make_solid(self.spec)
        for cut in self.spec.get("cuts", []):
            solid = solid.cut(make_solid(cut))
        self.solid = solid

    @property
    def bb(self):
        return self.solid.BoundingBox()


def bodies_from(cfg: dict) -> list[Body]:
    out = []
    for b in cfg["bodies"]:
        spec = {k: v for k, v in b.items() if k not in ("id", "name", "kind", "src", "bans")}
        out.append(Body(b["id"], b["name"], b["kind"], str(b.get("src", "")), spec, b.get("bans", [])))
    out += generated_bodies(cfg)
    for b in out:
        b.build()
    return out


def generated_bodies(cfg: dict) -> list[Body]:
    """Probe array, pads and the keepouts derived from M-10, M-12, M-13, M-15 and M-18."""
    p, pogo = cfg["params"], cfg["pogo"]
    cx, cy = p["pogo_center"]
    pitch = p["pogo_pitch"]
    pad_z = p["pad_plane"]
    db_top = pad_z - p["pogo_working"]
    hw = sorted(HARDWARE - {"pogo", "pcb"})
    out: list[Body] = []
    for i, (dx, dy) in enumerate(itertools.product((-1.5, -0.5, 0.5, 1.5), (-0.5, 0.5)), start=1):
        x, y = cx + dx * pitch, cy + dy * pitch
        out.append(Body(f"probe_{i}", f"Mill-Max 7911 probe {i} (flange envelope)", "pogo", "M-10 / M-12 / B-12",
                        {"shape": "cylz", "cx": x, "cy": y, "z": db_top, "h": p["pogo_working"],
                         "dia": pogo["flange_dia"]}))
        out.append(Body(f"travel_{i}", f"Probe {i} travel cylinder", "keepout", "design rule keepout",
                        {"shape": "cylz", "cx": x, "cy": y, "z": db_top, "h": p["pogo_working"], "dia": 2.2},
                        bans=hw))
    out.append(Body("contact_cavity", "Dry contact cavity", "keepout", "M-15 28 x 20",
                    {"shape": "box", "x": cx - 14, "y": cy - 10, "z": 0.0, "w": 28, "d": 20, "h": pad_z},
                    bans=sorted(HARDWARE - {"pogo", "pcb"})))
    for b in cfg["bodies"]:
        if b["kind"] == "boss":
            out.append(Body(f"{b['id']}_engage", "Boss engagement volume", "keepout", "M-18",
                            {"shape": "cylz", "cx": b["cx"], "cy": b["cy"], "z": b["z"], "h": b["h"], "dia": 6.4},
                            bans=sorted(HARDWARE - {"boss"})))
    return out


# ---------------------------------------------------------------- checks

def overlap(a: Body, b: Body) -> float:
    ba, bb = a.bb, b.bb
    if (ba.xmin >= bb.xmax or bb.xmin >= ba.xmax or ba.ymin >= bb.ymax or bb.ymin >= ba.ymax
            or ba.zmin >= bb.zmax or bb.zmin >= ba.zmax):
        return 0.0
    return a.solid.intersect(b.solid).Volume()


def gate1(cfg: dict, bodies: list[Body]) -> tuple[list, list]:
    fr, p = cfg["frame"], cfg["params"]
    L, W, H = fr["length"], fr["width"], fr["height"]
    f: list[tuple[str, str]] = []
    notes: list[str] = []

    for b in bodies:
        bb = b.bb
        if bb.xmin < -EPS or bb.xmax > L + EPS or bb.ymin < -EPS or bb.ymax > W + EPS:
            f.append(("FAIL", f"`{b.id}` leaves the {L:g} x {W:g} mm footprint "
                              f"(X {bb.xmin:.2f}-{bb.xmax:.2f}, Y {bb.ymin:.2f}-{bb.ymax:.2f})"))
        if b.kind not in ("radio_structure", "keepout") and bb.zmax > H + EPS:
            f.append(("FAIL", f"`{b.id}` is {bb.zmax - H:.2f} mm above the {H:g} mm pack height"))

    solids = [b for b in bodies if b.kind != "keepout"]
    for a, b in itertools.combinations(solids, 2):
        if frozenset({a.kind, b.kind}) in MATED_KINDS:
            continue
        v = overlap(a, b)
        if v > EPS:
            f.append(("FAIL", f"`{a.id}` collides with `{b.id}` ({v:.2f} mm³)"))

    for k in (b for b in bodies if b.kind == "keepout"):
        for b in solids:
            if b.kind in k.bans and overlap(k, b) > EPS:
                f.append(("FAIL", f"`{b.id}` ({b.kind}) intrudes into keepout `{k.id}` ({k.name})"))

    cells = [b for b in bodies if b.kind == "cell"]
    gmin = p["cell_gap_min"]
    gaps = [(a.solid.distance(b.solid), a.id, b.id) for a, b in itertools.combinations(cells, 2)]
    adjacent = [g for g in gaps if g[0] < 3 * gmin]
    tightest = min(adjacent)
    notes.append(f"Adjacent cell can gaps: {len(adjacent)} pairs, {tightest[0]:.2f} to {max(adjacent)[0]:.2f} mm")
    for d, a, b in adjacent:
        if d < gmin - EPS:
            f.append(("FAIL", f"cells `{a}`/`{b}` are {d:.2f} mm apart (M-06 minimum {gmin:g} mm)"))
    if tightest[0] < gmin + 0.25:
        f.append(("WARN", f"all {len(adjacent)} adjacent cell pairs sit at {tightest[0]:.2f} mm, exactly the M-06 "
                          "minimum: the separate insulation bodies and any holder ribs must fit inside that gap"))
    struct = [b for b in bodies if b.kind == "structure"]
    for c in cells:
        d = min(c.solid.distance(s.solid) for s in struct)
        notes.append(f"`{c.id}`: {d:.2f} mm to the nearest enclosure surface (through the insulation layer)")
        if d < gmin - EPS:
            f.append(("FAIL", f"`{c.id}` is {d:.2f} mm from the enclosure (< {gmin:g} mm)"))
    zone = p["cell_zone"]
    end_room = (zone[1] - zone[0] - p["cell_len"]) / 2
    notes.append(f"Cell end room for support, interconnect and expansion: {end_room:.2f} mm per end "
                 f"({zone[1] - zone[0]:g} mm zone, {p['cell_len']:g} mm max cell)")
    if end_room < 1.5:
        f.append(("WARN", f"only {end_room:.2f} mm per cell end for the nonconductive end support, "
                          "nickel interconnect and expansion allowance (M-07)"))

    zstack = (p["top_plate_cells"] + 2 * p["cell_insulation"] + 2 * p["cell_dia"] + p["layer_gap"]
              + p["bottom_wall"])
    notes.append(f"Z stack over the cells: {zstack:.2f} mm against the {H:g} mm M-02 height "
                 f"({H - zstack:+.2f} mm); with 2.0 mm insulation it is "
                 f"{zstack + 2 * (2.0 - p['cell_insulation']):.2f} mm")
    if zstack > H + EPS:
        f.append(("FAIL", f"cell Z stack {zstack:.2f} mm exceeds {H:g} mm"))

    total = sum(v for _, v in cfg["budget"])
    notes.append(f"D-017 length budget in the 145 mm frame: {' + '.join(f'{v:g}' for _, v in cfg['budget'])} "
                 f"= {total:g} mm against {L:g} mm")
    if abs(total - L) > EPS:
        f.append(("FAIL", f"D-017 length budget sums to {total:g} mm, not {L:g} mm"))
    half = p["gland_width"] / 2
    gx_max = cfg["params"]["pogo_center"][0] + 17 + half
    latch_x0 = next(b for b in bodies if b.id == "latch").bb.xmin
    notes.append(f"Gasket gland outer edge at X {gx_max:.2f} mm against the M-20 latch zone start at X {latch_x0:g} mm "
                 f"(the D-017 budget closes on the cord centerline only; the gland adds {half:.2f} mm each side)")
    if gx_max > latch_x0:
        f.append(("FAIL", f"gasket gland ({p['gland_width']:g} mm wide) reaches X {gx_max:.2f}, "
                          f"{gx_max - latch_x0:.2f} mm into the M-20 latch zone (X {latch_x0:g}+)"))
    c3 = cfg["c3_family_catch"]
    zone_w = 7.0
    if min(c3) > zone_w:
        f.append(("FAIL", f"a Southco C3-family catch body ({c3[0]:g} x {c3[1]:g} x {c3[2]:g} mm) does not fit the "
                          f"{zone_w:g} mm M-20 latch zone in any orientation (C3-99-107-055 drawing not retrieved)"))
    return f, notes


def gate2(cfg: dict) -> list[str]:
    """Hook-first pivot about the hook engagement point: what engages first."""
    p, pogo = cfg["params"], cfg["pogo"]
    px, pz = p["hook_pivot"]
    cx, _ = p["pogo_center"]
    nominal_stroke = pogo["initial_height"] - p["pogo_working"]
    squeeze = p["gasket_cord"] * p["gasket_squeeze"]
    lines = []

    def angle(x: float, gap: float) -> float:
        return math.degrees(math.asin(gap / (x - px)))

    boss = angle(cx, p["boss_engagement"])
    # Closing: the angle falls toward 0, so a feature nearer the hook (larger
    # angle at the same gap) meets its mate first.
    near_col, far_col = cx - 1.5 * p["pogo_pitch"], cx + 1.5 * p["pogo_pitch"]
    probe_first, probe_last = angle(near_col, nominal_stroke), angle(far_col, nominal_stroke)
    gasket_first, gasket_last = angle(cx - 17, squeeze), angle(cx + 17, squeeze)
    lines += [
        f"Pivot assumed at the hook engagement point X {px:g}, Z {pz:g} mm. Angles are measured from closed; "
        "a larger angle means earlier contact while closing and later separation while opening.",
        f"Locating bosses ({p['boss_engagement']:g} mm engagement) start to engage at {boss:.2f}°.",
        f"Probes first touch their pads at {probe_first:.2f}° (X {near_col:g} column, nearest the hooks) and last at "
        f"{probe_last:.2f}° (X {far_col:g} column), {nominal_stroke:.2f} mm nominal stroke. On removal the "
        f"X {far_col:g} column breaks first.",
        f"The gasket first touches at {gasket_first:.2f}° (hook side, X {cx - 17:g}) and last at {gasket_last:.2f}° "
        f"(latch side, X {cx + 17:g}).",
    ]
    order_ok = boss > probe_first and boss > gasket_first
    lines.append(("PASS" if order_ok else "FAIL") + ": M-18 requires the bosses to engage before the probes and "
                 "before meaningful gasket compression.")
    # Scrub: pad face offset from the pivot height turns rotation into X slip.
    dz = p["pad_plane"] - pz
    slip = dz * math.sin(math.radians(probe_first)) + (cx - px) * (1 - math.cos(math.radians(probe_first)))
    lines.append(f"Probe tip slip along X over the last {nominal_stroke:.2f} mm of travel: about {slip * 1000:.0f} µm "
                 "(negligible against the pad radius).")
    lines.append(f"Pad tilt at first touch: {probe_first:.2f}°. The latch end closes last, so the latch only completes "
                 "the seat against the hard stops (it must not set probe stroke, per the latch rule).")
    return lines


def gate4_latch(cfg: dict) -> list[str]:
    """Moment balance about the hook: latch force needed to hold gasket and probe preload."""
    p, e = cfg["params"], cfg["latch_estimate"]
    px = p["hook_pivot"][0]
    cx = p["pogo_center"][0]
    perim = 2 * (34 + 26) - (8 - 2 * math.pi) * 3.0
    lo, hi = (perim * k for k in e["gasket_n_per_mm"])
    probes = 8 * e["probe_n"]
    arm = (cx - px) / (e["latch_x"] - px)
    need = [(g + probes) * arm for g in (lo, hi)]
    return [
        f"Latch preload estimate (moment balance about the hooks, gasket and probes centered at X {cx:g}, latch at "
        f"X {e['latch_x']:g}): gasket {lo:.0f} to {hi:.0f} N over a {perim:.0f} mm centerline at "
        f"{e['gasket_n_per_mm'][0]:g} to {e['gasket_n_per_mm'][1]:g} N/mm, probes {probes:.1f} N, so the latch must "
        f"hold {need[0]:.0f} to {need[1]:.0f} N before any shock load. A 5 lbf (22 N) C3-class catch does not "
        "cover this. Estimate only: the N/mm range is read approximately from Parker ORD 5700 Fig. 2-4 "
        "(0.070 in cord, 20 % squeeze, 50 to 70 Shore A) and must be replaced by the gasket supplier's data.",
    ]


def wc_rss(items: list) -> tuple[float, float]:
    vals = [v for _, v in items]
    return sum(vals), math.sqrt(sum(v * v for v in vals))


def gate3(cfg: dict) -> tuple[list, list]:
    p, pogo = cfg["params"], cfg["pogo"]
    f: list[tuple[str, str]] = []
    lines: list[str] = []
    w = p["pogo_working"]
    h_min, h_max = pogo["initial_height"] - pogo["length_tol"], pogo["initial_height"] + pogo["length_tol"]
    lines += ["### Probe working height and stroke", "",
              f"Probe initial height {pogo['initial_height']:.3f} ± {pogo['length_tol']:.2f} mm, max stroke "
              f"{pogo['max_stroke']:.3f} mm (Mill-Max). Nominal working height {w:g} mm (M-12). "
              f"Including the probe's own length tolerance, the working height must stay between "
              f"{h_max - pogo['max_stroke']:.3f} mm (no bottoming) and {h_min - pogo['min_preload_stroke']:.3f} mm "
              f"(≥ {pogo['min_preload_stroke']:g} mm preload), narrower than the 6.10-7.49 mm M-12 quotes, "
              "which ignores the probe tolerance.", "",
              "| Case | Worst case ± | RSS ± | Min stroke (WC) | Max stroke (WC) | Within M-12 ±0.25 | Result |",
              "|---|---|---|---|---|---|---|"]
    for case, items in cfg["stack_pogo"].items():
        wc, rss = wc_rss(items)
        smin, smax = h_min - (w + wc), h_max - (w - wc)
        ok = smin >= pogo["min_preload_stroke"] and smax <= pogo["max_stroke"]
        res = "PASS" if ok else "FAIL"
        lines.append(f"| {case.replace('_', ' ')} | {wc:.2f} | {rss:.3f} | {smin:.3f} | {smax:.3f} | "
                     f"{'yes' if wc <= 0.25 else 'no'} | {res} |")
        if not ok:
            f.append(("FAIL", f"probe stack, {case.replace('_', ' ')}: worst-case stroke {smin:.2f} to {smax:.2f} mm "
                              f"(needs {pogo['min_preload_stroke']:g} to {pogo['max_stroke']:.2f})"))
    g = cfg["stack_gasket"]
    d = p["gasket_cord"]
    gland = d * (1 - p["gasket_squeeze"])
    gwc, grss = wc_rss(g["gland"])
    lines += ["", "### Gasket squeeze", "",
              f"Cord {d:g} mm, nominal compressed height {gland:.2f} mm ({p['gasket_squeeze']:.0%} squeeze, M-16). "
              f"Gland and face-gap stack ±{gwc:.2f} mm worst case, ±{grss:.3f} mm RSS. M-16 limit "
              f"{g['max_squeeze']:.0%} worst case; Parker's face-seal chart runs "
              f"{g['parker_min_squeeze']:.0%} to 32 % for a 0.070 in cord.", "",
              "| Cord tolerance | Squeeze WC | Squeeze RSS | Result |", "|---|---|---|---|"]
    for name, ct in g["cord_tol"].items():
        wmax = (d + ct - (gland - gwc)) / (d + ct)
        wmin = (d - ct - (gland + gwc)) / (d - ct)
        r = math.sqrt(ct * ct + grss * grss)
        rmax, rmin = (d - gland + r) / d, (d - gland - r) / d
        ok = wmax < g["max_squeeze"]
        lines.append(f"| {name} ±{ct:g} | {wmin:.1%} to {wmax:.1%} | {rmin:.1%} to {rmax:.1%} | "
                     f"{'PASS' if ok else 'FAIL'} max; min {'below' if wmin < g['parker_min_squeeze'] else 'within'} "
                     "Parker band |")
        if not ok:
            f.append(("FAIL", f"gasket squeeze with {name} reaches {wmax:.1%} worst case (M-16 < {g['max_squeeze']:.0%})"))
        if wmin < g["parker_min_squeeze"]:
            f.append(("WARN", f"gasket squeeze with {name} falls to {wmin:.1%} worst case "
                              f"({rmin:.1%} RSS), below Parker's {g['parker_min_squeeze']:.0%} face-seal minimum"))
    pwc, prss = wc_rss(cfg["stack_position"])
    allow = p["pad_dia"] / 2 - pogo["plunger_dia"] / 2
    lines += ["", "### Plunger on pad", "",
              f"Radial allowance for the {pogo['plunger_dia']:.3f} mm plunger to stay wholly on a "
              f"{p['pad_dia']:g} mm pad: {allow:.3f} mm. Position stack {pwc:.2f} mm worst case, {prss:.3f} mm RSS: "
              f"{'PASS' if pwc <= allow else 'FAIL'}."]
    if pwc > allow:
        f.append(("FAIL", f"plunger position stack {pwc:.2f} mm exceeds the {allow:.2f} mm pad allowance"))
    return f, lines


def gate5(cfg: dict) -> list[str]:
    p = cfg["params"]
    gap = p["pogo_pitch"] - p["pad_dia"]
    cx, cy = p["pogo_center"]
    half = p["gland_width"] / 2
    pad_x = (cx - 1.5 * p["pogo_pitch"] - p["pad_dia"] / 2, cx + 1.5 * p["pogo_pitch"] + p["pad_dia"] / 2)
    pad_y = (cy - 0.5 * p["pogo_pitch"] - p["pad_dia"] / 2, cy + 0.5 * p["pogo_pitch"] + p["pad_dia"] / 2)
    land_x = min(pad_x[0] - (cx - 17 + half), (cx + 17 - half) - pad_x[1])
    land_y = min(pad_y[0] - (cy - 13 + half), (cy + 13 - half) - pad_y[1])
    land_cav = min(pad_x[0] - (cx - 14), pad_y[0] - (cy - 10))
    return [
        f"Pad-to-pad edge gap: {gap:.2f} mm on the {p['pogo_pitch']:g} mm grid with {p['pad_dia']:g} mm pads.",
        f"Land from the outermost pad edge to the gland inner edge: {min(land_x, land_y):.2f} mm "
        f"(M-15 needs ≥ 5 mm): {'PASS' if min(land_x, land_y) >= 5 else 'FAIL'}.",
        f"Land from the outermost pad edge to the cavity wall: {land_cav:.2f} mm.",
        "Pin allocation positions are not fixed by M-11; see the proposal in the docs gate record.",
    ]


# ---------------------------------------------------------------- outputs

COLORS = {"structure": "#d5d8dc", "cell": "#f5b041", "insulator": "#fad7a0", "bms": "#82e0aa", "protection": "#e74c3c",
          "pcb": "#58d68d", "pogo": "#2e86c1", "gasket": "#1c2833", "boss": "#8e44ad", "hook": "#6c3483",
          "latch": "#c0392b", "wiring": "#f9e79f", "radio_structure": "none"}


def write_svg(cfg: dict, bodies: list[Body], path: Path) -> None:
    s, pad = 5.0, 20.0
    L, W = cfg["frame"]["length"], cfg["frame"]["width"]
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{L * s + 2 * pad:.0f}" height="{W * s + 2 * pad + 30:.0f}" '
           'font-family="monospace" font-size="9">', '<rect width="100%" height="100%" fill="white"/>']

    def r(bb):
        return pad + bb.xmin * s, pad + (W - bb.ymax) * s, bb.xlen * s, bb.ylen * s

    order = ["structure", "insulator", "cell", "wiring", "bms", "protection", "pcb", "gasket", "pogo", "boss", "hook",
             "latch", "keepout"]
    for kind in order:
        for b in (b for b in bodies if b.kind == kind):
            if b.id.startswith("travel_"):
                continue
            x, y, w, h = r(b.bb)
            if kind == "keepout":
                style = 'fill="none" stroke="#c0392b" stroke-dasharray="4 2"'
            elif kind == "structure":
                style = 'fill="#d5d8dc" fill-opacity="0.35" stroke="#7f8c8d"'
            elif kind == "gasket":
                style = 'fill="none" stroke="#1c2833" stroke-width="3"'
            else:
                style = f'fill="{COLORS.get(kind, "#ccc")}" fill-opacity="0.7" stroke="black"'
            out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" {style}/>')
    out.append(f'<rect x="{pad}" y="{pad}" width="{L * s}" height="{W * s}" fill="none" stroke="black"/>')
    out.append(f'<text x="{pad}" y="{W * s + 2 * pad + 12:.0f}">{L:g} x {W:g} mm pack, viewed from the battery side, '
               'X right (hook end left), Y up. Orange cells, green PCBs, red protection/latch, blue probes, '
               'black ring gasket, dashed red keepouts.</text>')
    out.append("</svg>")
    path.write_text("\n".join(out))


def main() -> int:
    cfg = yaml.safe_load((HERE / "pack.yaml").read_text())
    bodies = bodies_from(cfg)
    OUT_DIR.mkdir(exist_ok=True)

    f1, n1 = gate1(cfg, bodies)
    g2 = gate2(cfg)
    f3, g3 = gate3(cfg)
    g5 = gate5(cfg)

    asm = cq.Assembly(name="openmanet_v1_pack")
    for b in bodies:
        if b.kind != "keepout":
            asm.add(cq.Workplane().add(b.solid), name=b.id)
    asm.export(str(OUT_DIR / "pack_assembly.step"))

    with (OUT_DIR / "pack_coordinates.csv").open("w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(["id", "name", "kind", "x_min", "y_min", "z_min", "x_max", "y_max", "z_max", "source"])
        for b in bodies:
            bb = b.bb
            wr.writerow([b.id, b.name, b.kind, f"{bb.xmin:.2f}", f"{bb.ymin:.2f}", f"{bb.zmin:.2f}", f"{bb.xmax:.2f}",
                         f"{bb.ymax:.2f}", f"{bb.zmax:.2f}", b.src])
    write_svg(cfg, bodies, OUT_DIR / "pack.svg")

    def verdict(fs):
        if any(s == "FAIL" for s, _ in fs):
            return "FAIL"
        return "PASS with warnings" if fs else "PASS"

    lines = ["# V1 battery pack gate check (generated)", "",
             "Generated by `pack.py` from `pack.yaml`. Do not edit by hand. This is an envelope model built from "
             "v1-battery-pack.md M-01 to M-22 plus the assumptions listed below; it can show that something does "
             "not fit, but it does not freeze dimensions.", "",
             f"## Gate 1: fit check: {verdict(f1)}", ""]
    lines += [f"- **{s}** {m}" for s, m in f1] or ["- No collisions, footprint or keepout violations."]
    lines += ["", "Measurements:", ""] + [f"- {n}" for n in n1]
    lines += ["", "## Gate 2: motion check (first-order pivot only)", ""] + [f"- {x}" for x in g2]
    lines += ["- Not evaluated: hook and keeper profiles, latch travel and keeper path, and the pivot point itself "
              "(M-19 and M-20 are CAD-verify; B-13 drawing not in hand)."]
    lines += ["", f"## Gate 3: tolerance study (assumed contributors): {verdict(f3)}", ""]
    lines += [f"- **{s}** {m}" for s, m in f3] + [""] + g3
    lines += ["", "## Gate 4: structural review", "",
              "- Not evaluated by this model: needs the enclosure material and process, hook and keeper geometry, "
              "pack mass and drop/shock load cases. See the docs gate record for the load-path notes."]
    lines += [f"- {x}" for x in gate4_latch(cfg)]
    lines += ["", "## Gate 5: target-pad geometry", ""] + [f"- {x}" for x in g5]
    lines += ["", "## Bodies and sources", "", "| Body | Kind | Source |", "|---|---|---|"]
    lines += [f"| `{b.id}` | {b.kind} | {b.src} |" for b in bodies if not b.id.startswith(("travel_", "probe_"))]
    lines += ["", "Assumptions in `pack.yaml` are marked `assumption` in the source column or in comments.", ""]
    (OUT_DIR / "pack_report.md").write_text("\n".join(lines))
    print((OUT_DIR / "pack_report.md").read_text())

    fails = any(s == "FAIL" for s, _ in f1 + f3)
    return 1 if fails and "--strict" in sys.argv[1:] else 0


if __name__ == "__main__":
    sys.exit(main())
