"""Preserve routed topology while spreading and rounding shared copper paths."""
from collections import defaultdict
from dataclasses import replace
import math

from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .model import LAYERS, Primitive, Route, Via
from .router import Scene, fillet, relax, simplify


def import_copper(board, data):
    ids = {name: net for net, name in board.nets.items()}
    primitives = [Primitive(ids[t["net"]], LAYERS.index(t["layer"]), t["width"],
                            tuple(t["start"]), tuple(t["end"]),
                            tuple(t["mid"]) if t.get("mid") is not None else None)
                  for t in data["tracks"]]
    vias = [Via(ids[v["net"]], tuple(v["center"]), v["diameter"], v["drill"]) for v in data["vias"]]
    return primitives, vias


def topology_paths(board, primitives, vias):
    """Node T-junctions and pad/via contacts before changing any centerline.

    Shared branch points, terminals and all width changes remain fixed.
    Net names and layers are never inferred from proximity.
    """
    result = []
    for net, layer in sorted({(p.net, p.layer) for p in primitives}):
        items = [p for p in primitives if p.net == net and p.layer == layer]
        widths = {p.width for p in items}
        if len(widths) != 1 or any(p.mid is not None for p in items):
            raise ValueError("Seed routing must have one straight-track width per net/layer")
        width = next(iter(widths))
        contacts = [p.shape for p in board.pads if p.plated and p.net == net and layer in p.layers]
        contacts += [Point(v.center).buffer(v.diameter/2) for v in vias if v.net == net]
        contact = unary_union(contacts)
        network = unary_union([p.line() for p in items])
        edges, adjacency, fixed = [], defaultdict(list), set()
        def key(p):
            return tuple(round(float(x), 6) for x in p)
        for line in getattr(network, "geoms", [network]):
            coords = list(line.coords)
            for a, b in zip(coords, coords[1:]):
                segment = LineString([a, b])
                cuts = [0, segment.length]
                intersection = segment.intersection(contact.boundary)
                for part in getattr(intersection, "geoms", [intersection]):
                    if part.is_empty:
                        continue
                    if part.geom_type == "Point":
                        cuts.append(segment.project(part))
                    elif part.geom_type == "LineString":
                        cuts.extend(segment.project(Point(pt)) for pt in (part.coords[0], part.coords[-1]))
                cuts = sorted(set(round(d, 9) for d in cuts))
                for start, end in zip(cuts, cuts[1:]):
                    aa, bb = key(segment.interpolate(start).coords[0]), key(segment.interpolate(end).coords[0])
                    if math.dist(aa, bb) < 1e-6:
                        continue
                    index = len(edges)
                    edges.append((aa, bb))
                    adjacency[aa].append(index)
                    adjacency[bb].append(index)
                    for point in (aa, bb):
                        if contact.distance(Point(point)) < 2e-6:
                            fixed.add(point)
        fixed.update(p for p, edge_ids in adjacency.items() if len(edge_ids) != 2)
        used = set()
        def walk(start, first):
            points, current, edge = [start], start, first
            while edge not in used:
                used.add(edge)
                a, b = edges[edge]
                current = b if current == a else a
                points.append(current)
                if current in fixed:
                    break
                edge = next((i for i in adjacency[current] if i not in used), edge)
            return points
        for start in sorted(fixed):
            for edge in adjacency[start]:
                if edge not in used:
                    result.append((net, layer, width, walk(start, edge)))
        for edge, (start, _) in enumerate(edges):
            if edge not in used:
                result.append((net, layer, width, walk(start, edge)))
    return result


def finish(board, primitives, vias, options, progress=None):
    paths = topology_paths(board, primitives, vias)
    routes = []
    for net in sorted({p.net for p in primitives}):
        routes.append(Route(net, [(layer, points) for n, layer, _, points in paths if n == net],
                            [v for v in vias if v.net == net]))
    scene = Scene(board, routes)
    changed, kept = 0, 0
    for route in routes:
        result, generated = [], []
        for layer in (0, 1):
            forbidden = scene.obstacles(route.net, layer)
            allowed = scene.allowed(route.net)
            for route_layer, points in route.paths:
                if route_layer != layer:
                    continue
                original = LineString(points)
                if not Scene.legal(original, forbidden, allowed):
                    # A native-valid seed can sit on the conservative EPS margin.
                    # Preserve it and expose this in the report, never move blindly.
                    kept += 1
                    result.append((layer, points))
                    generated.extend(Primitive(route.net, layer, board.rules[route.net].width, a, b)
                                     for a, b in zip(points, points[1:]))
                    continue
                simple = simplify(points, forbidden, allowed)
                spread = relax(simple, forbidden, allowed, options)
                if spread != points:
                    changed += 1
                result.append((layer, spread))
                generated.extend(fillet(spread, route.net, layer, board.rules[route.net].width,
                                        forbidden, allowed, options.radius, angle_threshold=0.001))
        route.paths, route.primitives = result, generated
        if progress:
            progress(f"Shape {board.nets[route.net]}: {len(generated)} copper primitives")
    return ([p for r in routes for p in r.primitives], vias,
            dict(topological_paths=len(paths), spread_paths=changed, preserved_margin_paths=kept,
                 options=vars(replace(options))))
