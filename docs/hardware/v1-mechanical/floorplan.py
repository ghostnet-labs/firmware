#!/usr/bin/env python3
"""OpenMANET V1 carrier floorplan model and clearance check (GHO-7).

Builds the 117 x 67 mm carrier assembly from parts.yaml, using manufacturer
STEP models from step/ where present and documented envelopes otherwise, then
checks:

  * board-edge clearance (handoff section 27)
  * pairwise 3D collisions and minimum clearance between placed parts
  * keepout rules (no inductors/regulators/connectors under the CM5)
  * the free space left on each band for parts that are still blocked

Outputs (in out/): report.md, coordinates.csv, floorplan.svg, assembly.step.

Usage:  pip install cadquery pyyaml && python3 floorplan.py
"""

from __future__ import annotations

import csv
import itertools
import sys
from dataclasses import dataclass, field
from pathlib import Path

import cadquery as cq
import yaml

HERE = Path(__file__).resolve().parent
STEP_DIR = HERE / "step"
OUT_DIR = HERE / "out"

# Pairs that touch by design (card plugs into its socket).
MATED = {frozenset({"halow_socket", "halow_card"}), frozenset({"wifi_socket", "wifi_card"})}
# Minimum part-to-part gap reported as a warning (assembly/rework margin).
MIN_GAP = 0.5


@dataclass
class Part:
    id: str
    name: str
    kind: str
    status: str
    source: str
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    d: float = 0.0
    z0: float = 0.0
    h: float = 0.0
    step: str | None = None
    step_offset: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    step_rot: float = 0.0
    bans: list[str] = field(default_factory=list)
    edge_ok: bool = False
    shape: cq.Shape | None = None
    from_step: bool = False
    pieces: list = field(default_factory=list)  # (solid, bounding box) pairs

    @property
    def placed(self) -> bool:
        return self.status != "blocked"

    def build(self) -> None:
        if not self.placed:
            return
        path = STEP_DIR / self.step if self.step else None
        if path and path.exists():
            shape = cq.importers.importStep(str(path)).val()
            shape = shape.rotate((0, 0, 0), (0, 0, 1), self.step_rot).translate(
                cq.Vector(self.x, self.y, self.z0) + cq.Vector(*self.step_offset)
            )
            self.shape = shape
            self.from_step = True
            bb = shape.BoundingBox()
            self.x, self.y, self.z0 = bb.xmin, bb.ymin, bb.zmin
            self.w, self.d, self.h = bb.xlen, bb.ylen, bb.zlen
            self.pieces = [(sol, sol.BoundingBox()) for sol in shape.Solids()]
            return
        self.shape = (
            cq.Workplane()
            .box(self.w, self.d, self.h, centered=False)
            .translate((self.x, self.y, self.z0))
            .val()
        )
        self.pieces = [(self.shape, self.shape.BoundingBox())]


def load() -> tuple[dict, list[Part]]:
    data = yaml.safe_load((HERE / "parts.yaml").read_text())
    parts = [Part(**p) for p in data["parts"]]
    for p in parts:
        p.build()
    return data["board"], parts


def bb_gap(p, q) -> float:
    dx = max(q.xmin - p.xmax, p.xmin - q.xmax, 0.0)
    dy = max(q.ymin - p.ymax, p.ymin - q.ymax, 0.0)
    dz = max(q.zmin - p.zmax, p.zmin - q.zmax, 0.0)
    return (dx * dx + dy * dy + dz * dz) ** 0.5


def exact_check(a: Part, b: Part) -> tuple[float, float]:
    """Overlap volume and minimum gap, testing only solids whose boxes are near."""
    vol, gap = 0.0, float("inf")
    for sa, ba in a.pieces:
        for sb, bb in b.pieces:
            if bb_gap(ba, bb) >= MIN_GAP:
                continue
            vol += sa.intersect(sb).Volume()
            gap = min(gap, sa.distance(sb))
    return vol, gap


def xy_overlap(a: Part, b: Part) -> bool:
    return a.x < b.x + b.w and b.x < a.x + a.w and a.y < b.y + b.d and b.y < a.y + a.d


def check(board: dict, parts: list[Part]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    bw, bd, clr = board["w"], board["d"], board["edge_clearance"]
    placed = [p for p in parts if p.placed]

    for p in placed:
        if p.kind == "keepout" or p.edge_ok:
            continue
        margins = {
            "left": p.x,
            "right": bw - (p.x + p.w),
            "bottom": p.y,
            "top": bd - (p.y + p.d),
        }
        for side, m in margins.items():
            if m < 0:
                findings.append(("FAIL", f"`{p.id}` overhangs the {side} board edge by {-m:.2f} mm"))
            elif m < clr:
                findings.append(("FAIL", f"`{p.id}` is {m:.2f} mm from the {side} edge (< {clr} mm wall clearance)"))

    solids = [p for p in placed if p.kind != "keepout"]
    for a, b in itertools.combinations(solids, 2):
        if frozenset({a.id, b.id}) in MATED:
            continue
        vol, gap = exact_check(a, b)
        if vol > 1e-6:
            findings.append(("FAIL", f"`{a.id}` collides with `{b.id}` ({vol:.1f} mm³ overlap)"))
            continue
        if gap < MIN_GAP:
            findings.append(("WARN", f"`{a.id}` and `{b.id}` are {gap:.2f} mm apart (< {MIN_GAP} mm)"))

    for k in (p for p in placed if p.kind == "keepout"):
        for p in solids:
            if p.kind in k.bans and xy_overlap(k, p):
                findings.append(("FAIL", f"`{p.id}` ({p.kind}) intrudes into `{k.id}`"))
    return findings


def free_spans(board: dict, parts: list[Part], y0: float, y1: float) -> list[tuple[float, float]]:
    """Free top-side X intervals across the full height of a horizontal band."""
    clr = board["edge_clearance"]
    occupied = sorted(
        (p.x, p.x + p.w)
        for p in parts
        if p.placed and p.kind != "keepout" and p.z0 >= 0 and p.y < y1 and p.y + p.d > y0
    )
    spans, cursor = [], clr
    for a, b in occupied:
        if a > cursor:
            spans.append((cursor, a))
        cursor = max(cursor, b)
    if board["w"] - clr > cursor:
        spans.append((cursor, board["w"] - clr))
    return [s for s in spans if s[1] - s[0] > 0.01]


def write_svg(board: dict, parts: list[Part], path: Path) -> None:
    s, pad = 6.0, 20.0
    bw, bd = board["w"], board["d"]
    W, H = bw * s + 2 * pad, bd * s + 2 * pad + 40
    fill = {"envelope": "#a9dfbf", "drawing": "#d2b4de", "assumption": "#f9e79f"}

    def r(x, y, w, d):  # board mm -> svg px (Y flipped)
        return pad + x * s, pad + (bd - y - d) * s, w * s, d * s

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" '
        f'font-family="monospace" font-size="10">',
        f'<rect width="100%" height="100%" fill="white"/>',
    ]
    X, Y, w, h = r(0, 0, bw, bd)
    out.append(f'<rect x="{X}" y="{Y}" width="{w}" height="{h}" fill="#eef" stroke="black"/>')
    c = board["edge_clearance"]
    X, Y, w, h = r(c, c, bw - 2 * c, bd - 2 * c)
    out.append(f'<rect x="{X}" y="{Y}" width="{w}" height="{h}" fill="none" stroke="#888" stroke-dasharray="4 3"/>')
    for p in sorted((p for p in parts if p.placed), key=lambda p: p.kind != "keepout"):
        X, Y, w, h = r(p.x, p.y, p.w, p.d)
        if p.kind == "keepout":
            style = 'fill="none" stroke="#c0392b" stroke-dasharray="6 3"'
        else:
            color = "#7fb3d5" if p.from_step else fill[p.status]
            dash = ' stroke-dasharray="2 2"' if p.z0 < 0 else ""
            style = f'fill="{color}" fill-opacity="0.8" stroke="black"{dash}'
        out.append(f'<rect x="{X:.1f}" y="{Y:.1f}" width="{w:.1f}" height="{h:.1f}" {style}/>')
        if p.kind != "keepout":
            out.append(f'<text x="{X + 3:.1f}" y="{Y + 12:.1f}">{p.id}</text>')
    out.append(
        f'<text x="{pad}" y="{H - 22}">117 x 67 mm, top view, Y up. Blue = manufacturer STEP, purple = manufacturer '
        f'drawing, green = handoff envelope, yellow = assumption; dotted outline = bottom side.</text>'
    )
    out.append(
        f'<text x="{pad}" y="{H - 8}">Dashed grey = {c} mm wall clearance; dashed red = CM5 underside keepout. '
        f'Blocked parts (MC327-5, RF bulkheads) not drawn.</text>'
    )
    out.append("</svg>")
    path.write_text("\n".join(out))


def main() -> int:
    board, parts = load()
    OUT_DIR.mkdir(exist_ok=True)
    findings = check(board, parts)

    asm = cq.Assembly(name="openmanet_v1_carrier")
    asm.add(
        cq.Workplane().box(board["w"], board["d"], board["thickness"], centered=False)
        .translate((0, 0, -board["thickness"])),
        name="pcb",
    )
    for p in parts:
        if p.placed and p.kind not in ("keepout",):
            asm.add(cq.Workplane().add(p.shape), name=p.id)
    asm.export(str(OUT_DIR / "assembly.step"))

    with (OUT_DIR / "coordinates.csv").open("w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["id", "name", "status", "geometry", "x_min", "y_min", "x_max", "y_max", "z_min", "z_max", "source"])
        for p in parts:
            if not p.placed:
                wr.writerow([p.id, p.name, p.status, "none", "", "", "", "", "", "", p.source])
                continue
            geo = "manufacturer STEP" if p.from_step else "box"
            wr.writerow([p.id, p.name, p.status, geo, f"{p.x:.2f}", f"{p.y:.2f}", f"{p.x + p.w:.2f}",
                         f"{p.y + p.d:.2f}", f"{p.z0:.2f}", f"{p.z0 + p.h:.2f}", p.source])

    write_svg(board, parts, OUT_DIR / "floorplan.svg")

    cm5 = next(p for p in parts if p.id == "cm5")
    clr = board["edge_clearance"]
    bands = {
        "lower band (below CM5)": (clr, cm5.y),
        "upper band (above CM5)": (cm5.y + cm5.d, board["d"] - clr),
    }
    steps_used = [p.id for p in parts if p.from_step]
    lines = [
        "# V1 carrier floorplan check (generated)",
        "",
        "Generated by `floorplan.py` from `parts.yaml`. Do not edit by hand.",
        "",
        f"Board {board['w']} × {board['d']} mm, {clr} mm wall clearance. "
        f"Manufacturer STEP models used: {', '.join(steps_used) if steps_used else 'none yet'}.",
        "",
        "## Findings",
        "",
    ]
    lines += [f"- **{sev}** {msg}" for sev, msg in findings] or ["- No collisions or clearance violations among placed parts."]
    lines += ["", "## Band space", ""]
    for name, (y0, y1) in bands.items():
        spans = free_spans(board, parts, y0, y1)
        desc = ", ".join(f"X {a:.1f}–{b:.1f} ({b - a:.1f} mm)" for a, b in spans) or "none"
        lines.append(f"- {name}: Y {y0:.1f}–{y1:.1f} ({y1 - y0:.1f} mm tall). Free full-height top-side X spans: {desc}.")
    top = [p for p in parts if p.placed and p.kind not in ("keepout", "mount") and p.z0 >= 0]
    tallest = max(top, key=lambda p: p.z0 + p.h)
    lines += [
        "",
        "## Height",
        "",
        f"- Tallest top-side part: `{tallest.id}` at {tallest.z0 + tallest.h:.2f} mm above the PCB. "
        "The enclosure's inner clear height above the PCB must exceed this plus lid clearance.",
    ]
    bottom = [p for p in parts if p.placed and p.kind != "keepout" and p.z0 < 0]
    if bottom:
        lines.append("- Bottom-side parts: " + ", ".join(f"`{p.id}`" for p in bottom) + ".")
    lines += ["", "## Not placed (blocked on geometry)", ""]
    lines += [f"- `{p.id}`: {p.name}. {p.source}" for p in parts if not p.placed]
    lines += ["", "## Assumptions still in the model", ""]
    lines += [f"- `{p.id}`: {p.source}" for p in parts if p.status == "assumption"]
    lines += ["", "## Geometry source per part", ""]
    for p in parts:
        if p.placed and p.kind != "keepout":
            src = "manufacturer STEP" if p.from_step else p.status
            lines.append(f"- `{p.id}`: {src}")
    lines.append("")
    (OUT_DIR / "report.md").write_text("\n".join(lines))

    print((OUT_DIR / "report.md").read_text())
    return 1 if any(sev == "FAIL" for sev, _ in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
