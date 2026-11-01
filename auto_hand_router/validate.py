"""Independent exported-copper connectivity and geometry checks; KiCad DRC is separate."""

from collections import Counter
from dataclasses import dataclass
import math

from shapely.geometry import Point
from shapely.strtree import STRtree

from .model import ARC_ERROR, read_copper
from .teardrops import read_teardrops


@dataclass
class Copper:
    net: int
    layers: tuple
    shape: object
    label: str
    generated: bool


def check(board, root):
    primitives, vias = read_copper(root, board.nets)
    items = [Copper(p.net, p.layers, p.shape, f"{p.ref}.{p.number}", False)
             for p in board.pads if p.plated]
    pad_indices = [i for i, p in enumerate(board.pads) if p.plated]
    for i, p in enumerate(primitives):
        items.append(Copper(p.net, (p.layer,), p.line().buffer(p.width / 2 + ARC_ERROR, quad_segs=24),
                            f"track-{i}", True))
    for i, v in enumerate(vias):
        items.append(Copper(v.net, (0, 1), Point(v.center).buffer(v.diameter / 2, quad_segs=48),
                            f"via-{i}", True))
    teardrops = read_teardrops(root, board.nets)
    for i, drop in enumerate(teardrops):
        items.append(Copper(drop.net, (drop.layer,), drop.shape, f"teardrop-{i}", True))
    errors, minimum_gap = [], math.inf
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i, j):
        parent[find(j)] = find(i)

    layer_ids = {layer:[i for i,item in enumerate(items) if layer in item.layers] for layer in (0,1)}
    layer_trees = {layer:STRtree([items[i].shape for i in ids]) for layer,ids in layer_ids.items()}
    max_clearance = max([r.clearance for r in board.rules.values()] + [1e-5])
    foreign_cache = {}
    def foreign_groups(net, layer):
        key = net, layer
        if key not in foreign_cache:
            groups = {}
            for j in layer_ids[layer]:
                item = items[j]
                if item.net == net:
                    continue
                required = max(board.rules[net].clearance,
                               board.rules.get(item.net, board.rules[net]).clearance)
                groups.setdefault(required, []).append(item.shape)
            foreign_cache[key] = [(required, STRtree(shapes)) for required,shapes in groups.items()]
        return foreign_cache[key]
    for i, a in enumerate(items):
        nearby = set()
        for layer in a.layers:
            indices = layer_trees[layer].query(a.shape, predicate="dwithin", distance=max_clearance)
            nearby.update(layer_ids[layer][int(j)] for j in indices if layer_ids[layer][int(j)] < i)
            if a.generated:
                for required,tree in foreign_groups(a.net, layer):
                    nearest = tree.nearest(a.shape)
                    if nearest is not None:
                        minimum_gap = min(minimum_gap, a.shape.distance(tree.geometries[nearest])-required)
        for j in sorted(nearby):
            b = items[j]
            if not set(a.layers).intersection(b.layers):
                continue
            gap = a.shape.distance(b.shape)
            if a.net == b.net and a.net:
                if gap < 1e-5:
                    join(i, j)
                continue
            if not (a.generated or b.generated):
                continue
            ra = board.rules.get(a.net)
            rb = board.rules.get(b.net)
            required = max(r.clearance for r in (ra, rb) if r is not None)
            if gap < required - 1e-5:
                errors.append({"type": "copper_clearance", "a": a.label, "b": b.label,
                               "gap_mm": gap, "required_mm": required})
    edge_regions = {net:board.boundary.buffer(-rule.edge) for net,rule in board.rules.items()}
    for item in items:
        if not item.generated:
            continue
        rules = board.rules[item.net]
        if not edge_regions[item.net].covers(item.shape):
            errors.append({"type": "board_edge", "item": item.label})
        for pad in board.pads:
            if not pad.drill or (pad.plated and item.net == pad.net):
                continue
            gap = item.shape.distance(pad.hole())
            if gap < rules.hole - 1e-5:
                errors.append({"type": "hole_clearance", "item": item.label,
                               "pad": f"{pad.ref}.{pad.number}", "gap_mm": gap})
        for area in board.rule_areas:
            blocks = area.vias if item.label.startswith("via-") else area.tracks
            if blocks and set(area.layers).intersection(item.layers) and item.shape.intersects(area.shape):
                errors.append({"type": "rule_area", "item": item.label})
    for i, via in enumerate(vias):
        rules = board.rules[via.net]
        copper = Point(via.center).buffer(via.diameter / 2, quad_segs=48)
        for ref, region in board.via_keepouts:
            if copper.distance(region) < rules.clearance - 1e-5:
                errors.append({"type": "via_in_footprint", "via": i, "footprint": ref})
        for pad in board.pads:
            if pad.plated and copper.distance(pad.shape) < rules.clearance - 1e-5:
                errors.append({"type": "via_in_pad", "via": i, "pad": f"{pad.ref}.{pad.number}"})
        if via.diameter < rules.via_diameter - 1e-6 or via.drill < rules.via_drill - 1e-6:
            errors.append({"type": "via_size", "via": i})
        holes = [p.hole() for p in board.pads if p.drill]
        holes += [Point(v.center).buffer(v.drill / 2, quad_segs=48) for v in vias[:i]]
        for hole in holes:
            gap = Point(via.center).distance(hole) - via.drill / 2
            if gap < rules.hole_to_hole - 1e-5:
                errors.append({"type": "hole_to_hole", "via": i, "gap_mm": gap})
    for i, p in enumerate(primitives):
        if p.width < board.rules[p.net].width - 1e-6:
            errors.append({"type": "track_width", "track": i})
    connectivity = []
    # Each pad/via is one item across its physical copper layers, so a PTH joins both.
    for net in sorted(n for n in board.nets if n):
        assigned = [i for i in range(len(pad_indices)) if items[i].net == net]
        if not assigned:
            continue
        components = len({find(i) for i in assigned})
        groups = {}
        for i in assigned:
            groups.setdefault(find(i), []).append(pad_indices[i])
        connectivity.append({"net": board.nets[net], "pads": len(assigned), "components": components,
                             "pad_groups": list(groups.values())})
    disconnected = sum(max(0, n["components"] - 1) for n in connectivity)
    gap_samples = []
    spacing_trees = {}
    for primitive in primitives:
        # Endpoint neighbourhoods are excluded: pin escape is necessarily denser.
        line = primitive.line()
        if line.length < 1.0:
            continue
        for t in (0.25, 0.5, 0.75):
            pt = line.interpolate(t, normalized=True)
            if min(pt.distance(p.shape) for p in board.pads if p.plated) < 0.35:
                continue
            key = primitive.net, primitive.layer
            if key not in spacing_trees:
                spacing_trees[key] = STRtree([item.shape for item in items if item.net != primitive.net
                                             and primitive.layer in item.layers and item.net])
            tree = spacing_trees[key]
            nearest = tree.nearest(pt)
            if nearest is not None:
                gap_samples.append(pt.distance(tree.geometries[nearest]) - primitive.width / 2)
    return {"passed": not errors and disconnected == 0,
            "geometry_errors": errors, "unconnected_components": disconnected,
            "connectivity": connectivity,
            "minimum_extra_clearance_mm": None if math.isinf(minimum_gap) else minimum_gap,
            "segments": sum(p.mid is None for p in primitives),
            "arcs": sum(p.mid is not None for p in primitives), "vias": len(vias), "teardrops": len(teardrops),
            "track_length_mm": sum(p.line().length for p in primitives),
            "layers_used": dict(Counter(p.layer for p in primitives)),
            "spacing_samples": {"count": len(gap_samples),
                                "mean_gap_mm": sum(gap_samples) / len(gap_samples) if gap_samples else None}}
