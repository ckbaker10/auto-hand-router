"""Exercise upstream routing core; rectangles conservatively bound original pads."""
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

from kicad_autorouter.model import Board, KeepoutArea, Layer, Pad, Point, Rules
from kicad_autorouter.router import Router, RouterConfig

data = json.loads(Path(sys.argv[1]).read_text())
layer = {0: Layer.FRONT, 1: Layer.BACK}
def points(sequence):
    return tuple(Point(*p) for p in sequence)
rules = {data["nets"][n]: Rules(clearance=r["clearance"], track_width=r["width"],
          via_diameter=r["via_diameter"], via_drill=r["via_drill"], edge_clearance=r["edge"])
         for n, r in data["rules"].items()}
b = Board(outline=points(data["outline"]), rules_by_net=rules,
          default_rules=rules[""], min_hole_to_hole=data["rules"]["0"]["hole_to_hole"],
          via_pad_hole_clearance=data["rules"]["0"]["hole"])
for p in data["pads"]:
    x1, y1, x2, y2 = p["bounds"]
    b.pads.append(Pad(id=p["id"], net=data["nets"][str(p["net"])] if p["plated"] else "",
                      center=Point(*p["center"]), width=x2-x1, height=y2-y1,
                      layers=frozenset(layer[i] for i in p["layers"]), drill=p["drill"],
                      component=p["component"], number=p["number"]))
for area in data["keepouts"]:
    b.keepouts.append(KeepoutArea(layers=frozenset(layer.values()),
                                  outline=points(area["clearance_outline"]), blocks_vias=True))
config = RouterConfig(grid=0.2, allow_partial=True)
start = time.time()
plan = Router(config).route(b)
elapsed = time.time() - start
result = dict(tracks=[dict(net=t.net, layer=0 if t.layer == Layer.FRONT else 1,
                          start=[t.start.x, t.start.y], end=[t.end.x, t.end.y], width=t.width)
                      for t in plan.tracks],
              vias=[dict(net=v.net, center=[v.position.x, v.position.y],
                         diameter=v.diameter, drill=v.drill) for v in plan.vias],
              failures=[asdict(f) for f in plan.failures], elapsed_seconds=elapsed,
              config=asdict(config), complete=plan.complete)
Path(sys.argv[2]).write_text(json.dumps(result, indent=2))
print(json.dumps({k: result[k] for k in ["elapsed_seconds", "complete", "failures"]}))
