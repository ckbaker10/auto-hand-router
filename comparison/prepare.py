"""Canonical unrouted input and loss-declared JSON for external core adapters."""
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import sys

from auto_hand_router.model import Board
from auto_hand_router.output import board_text

root = Path(__file__).resolve().parents[1]
out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
b = Board.load(root / "benchmarks/sources/555-clock/555_Timer_Clock.kicad_pcb")
(out / "555.kicad_pcb").write_text(board_text(b))
shutil.copyfile(b.project, out / "555.kicad_pro")
data = dict(nets=b.nets, rules={n: asdict(r) for n, r in b.rules.items()},
            outline=list(b.boundary.exterior.coords), pads=[], keepouts=[])
for p in b.pads:
    data["pads"].append(dict(id=f"{p.ref}.{p.number}", component=p.ref, number=p.number,
                            net=p.net, center=p.center, bounds=p.shape.bounds,
                            layers=p.layers, drill=p.drill, plated=p.plated))
for ref, region in b.via_keepouts:
    for shape in getattr(region, "geoms", [region]):
        if shape.geom_type == "Polygon":
            padded = shape.buffer(max(r.clearance for r in b.rules.values()))
            data["keepouts"].append(dict(ref=ref, outline=list(shape.exterior.coords),
                                          clearance_outline=list(padded.exterior.coords)))
(out / "555.json").write_text(json.dumps(data, indent=2))
print("Canonical input:", out)
