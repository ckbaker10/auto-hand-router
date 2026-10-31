"""Optional MIT-licensed negotiated-congestion core, with conservative geometry."""
from dataclasses import asdict
import time

from .model import EPS, Primitive, Via


def route(board, grid=.25):
    from kicad_autorouter.model import Board as CoreBoard, KeepoutArea, Layer, Pad, Point, Rules
    from kicad_autorouter.router import Router, RouterConfig
    layers = {0:Layer.FRONT, 1:Layer.BACK}
    def points(coords):
        return tuple(Point(*p) for p in coords)
    rules = {board.nets[n]: Rules(clearance=r.clearance+2*EPS, track_width=r.width,
              via_diameter=r.via_diameter, via_drill=r.via_drill, edge_clearance=r.edge+2*EPS)
             for n,r in board.rules.items()}
    core = CoreBoard(outline=points(board.boundary.exterior.coords), rules_by_net=rules,
                     default_rules=rules[""], min_hole_to_hole=max(r.hole_to_hole for r in board.rules.values())+2*EPS,
                     via_pad_hole_clearance=max(r.hole for r in board.rules.values())+2*EPS)
    for i,p in enumerate(board.pads):
        a,b,c,d = p.shape.bounds
        core.pads.append(Pad(id=f"{i}:{p.ref}.{p.number}", net=board.nets[p.net] if p.plated else "",
                             center=Point(*p.center), width=c-a, height=d-b,
                             layers=frozenset(layers[layer] for layer in p.layers), drill=p.drill,
                             component=p.ref, number=p.number))
    def area(shape, active, tracks, vias):
        for polygon in getattr(shape,"geoms",[shape]):
            if polygon.geom_type == "Polygon":
                core.keepouts.append(KeepoutArea(layers=frozenset(layers[i] for i in active),
                    outline=points(polygon.exterior.coords),
                    holes=tuple(points(ring.coords) for ring in polygon.interiors),
                    blocks_tracks=tracks, blocks_vias=vias))
    for _,shape in board.via_keepouts:
        area(shape.buffer(max(r.clearance for r in board.rules.values())+2*EPS),(0,1),False,True)
    for region in board.rule_areas:
        area(region.shape.buffer(EPS),region.layers,region.tracks,region.vias)
    for pad in board.pads:
        if pad.drill and not pad.plated:
            area(pad.hole().buffer(max(r.hole for r in board.rules.values())+2*EPS),(0,1),True,True)
    config = RouterConfig(grid=grid, allow_partial=True)
    start=time.time()
    plan=Router(config).route(core)
    names={name:net for net,name in board.nets.items()}
    tracks=[Primitive(names[t.net], 0 if t.layer==Layer.FRONT else 1, t.width,
                       (t.start.x,t.start.y),(t.end.x,t.end.y)) for t in plan.tracks]
    vias=[Via(names[v.net],(v.position.x,v.position.y),v.diameter,v.drill) for v in plan.vias]
    return tracks,vias,dict(name="J-Kreisel kicad-autorouter",version="0.1.9",
        host_seconds=time.time()-start,config=asdict(config),failures=[asdict(f) for f in plan.failures],
        adapter="Conservative pad rectangles, exact polygonal board/rule areas and padded via bodies")
