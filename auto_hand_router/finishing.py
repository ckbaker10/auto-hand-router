"""Preserve routed topology while spreading and rounding shared copper paths."""
from collections import defaultdict
from dataclasses import dataclass, replace
import math

from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .model import LAYERS, Primitive, Route, Via
from .router import Scene, relax, simplify
from .curves import smooth_path


def remove_single_layer_vias(board, primitives, vias):
    """A barrel touched on only one surface is an unnecessary shaping anchor."""
    from shapely.strtree import STRtree
    trees = {}
    for net in {v.net for v in vias}:
        for layer in (0,1):
            shapes = [p.line().buffer(p.width/2) for p in primitives if p.net==net and p.layer==layer]
            shapes += [p.shape for p in board.pads if p.plated and p.net==net and layer in p.layers]
            trees[net,layer] = STRtree(shapes)
    kept=[]
    for via in vias:
        disk=Point(via.center).buffer(via.diameter/2+1e-6)
        if all(len(trees[via.net,layer].query(disk,predicate="intersects")) for layer in (0,1)):
            kept.append(via)
    return kept


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
        contacts = [(p.shape, p.center) for p in board.pads if p.plated and p.net == net and layer in p.layers]
        contacts += [(Point(v.center).buffer(v.diameter/2), v.center) for v in vias if v.net == net]
        contact = unary_union([shape for shape,_ in contacts])
        network = unary_union([p.line() for p in items])
        edges, adjacency, fixed, seen_edges = [], defaultdict(list), set(), set()
        def key(p):
            return tuple(round(float(x), 6) for x in p)
        for line in getattr(network, "geoms", [network]):
            coords = list(line.coords)
            for a, b in zip(coords, coords[1:]):
                segment = LineString([a, b])
                cuts = [0, segment.length]
                for shape,center in contacts:
                    if segment.distance(shape) <= width/2 + 1e-6:
                        cuts.append(segment.project(Point(center)))
                cuts = sorted(set(round(d, 9) for d in cuts))
                for start, end in zip(cuts, cuts[1:]):
                    aa, bb = key(segment.interpolate(start).coords[0]), key(segment.interpolate(end).coords[0])
                    if math.dist(aa, bb) < 1e-6:
                        continue
                    edge_key = tuple(sorted((aa,bb)))
                    if edge_key in seen_edges:
                        continue
                    seen_edges.add(edge_key)
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


@dataclass
class ShapingPath:
    layer: int
    width: float
    points: list
    lead_start: bool = False
    lead_end: bool = False
    fixed: bool = False


def merge_fanins(board, net, paths, scene):
    """Move acute same-net joins away from a contact into a wider common stem."""
    import numpy as np
    paths = [replace(p,points=list(p.points)) for p in paths]
    nodes = defaultdict(list)
    for i,path in enumerate(paths):
        for end in (0,-1):
            nodes[path.layer,tuple(path.points[end])].append((i,end))
    merged = 0
    for (layer,node), arms in sorted(nodes.items()):
        contacts = [p.shape for p in board.pads if p.net==net and layer in p.layers and p.shape.covers(Point(node))]
        contacts += [Point(v.center).buffer(v.diameter/2) for r in scene.routes for v in r.vias
                     if v.net==net and math.dist(v.center,node)<1e-5]
        # A contact can absorb the stem's inner end. Moving a free T-junction
        # would create a new degree-two corner in its remaining arm.
        if len(arms)<2 or not contacts:
            continue
        directions = {}
        for index,end in arms:
            points = paths[index].points if end==0 else paths[index].points[::-1]
            line = LineString(points)
            vector = np.array(line.interpolate(min(1.8,line.length*.5)).coords[0])-node
            if np.linalg.norm(vector)>1e-5:
                directions[index,end] = vector/np.linalg.norm(vector)
        pairs = [(float(a@b), aa,bb) for aa,a in directions.items() for bb,b in directions.items()
                 if aa<bb and aa[0]!=bb[0] and float(a@b) > math.cos(math.radians(70))]
        if not pairs:
            continue
        _,aa,bb = max(pairs)
        axis = directions[aa]+directions[bb]
        axis /= np.linalg.norm(axis)
        origin = np.array(node)
        nominal = max(paths[aa[0]].width,paths[bb[0]].width)
        width = nominal*1.5
        extent = max([max(float((np.array(pt)-origin)@axis) for pt in shape.exterior.coords)
                      for shape in contacts]+[0])
        limit = min(1.8,*(LineString(paths[i].points).length*.3 for i,_ in (aa,bb)))
        accepted = None
        for distance in (limit,limit*.8,limit*.6,limit*.4):
            if distance < extent+.25:
                continue
            branch = origin+axis*distance
            lead = min(.45,distance*.3)
            proposals = []
            for i,end in (aa,bb):
                current = paths[i]
                points = current.points if end==0 else current.points[::-1]
                beyond = next((j for j in range(1,len(points)) if float((np.array(points[j])-origin)@axis)>distance+lead+.03),None)
                if beyond is None:
                    break
                new = [tuple(branch),tuple(branch+axis*lead)]+points[beyond:]
                candidate = replace(current,points=new if end==0 else new[::-1],
                                    lead_start=current.lead_start or end==0,
                                    lead_end=current.lead_end or end==-1)
                if not Scene.legal(LineString(candidate.points),scene.obstacles(net,layer,radius=candidate.width/2),
                                   scene.allowed(net,candidate.width/2)):
                    break
                proposals.append((i,candidate))
            if len(proposals)!=2:
                continue
            stem = ShapingPath(layer,width,[node,tuple(branch)],fixed=True)
            if Scene.legal(LineString(stem.points),scene.obstacles(net,layer,radius=width/2),scene.allowed(net,width/2)):
                accepted = proposals,stem
                break
        if accepted is not None:
            proposals,stem = accepted
            for index,candidate in proposals:
                paths[index]=candidate
            paths.append(stem)
            merged += 1
    return paths,merged


def finish(board, primitives, vias, options, progress=None):
    original_vias=len(vias)
    vias=remove_single_layer_vias(board,primitives,vias)
    paths = topology_paths(board, primitives, vias)
    routes = []
    for net in sorted({p.net for p in primitives}):
        routes.append(Route(net, [(layer, points) for n, layer, _, points in paths if n == net],
                            [v for v in vias if v.net == net]))
    scene = Scene(board, routes)
    changed, kept, merged = 0, 0, 0
    for route in routes:
        result, generated = [], []
        planned = [ShapingPath(layer,board.rules[route.net].width,points) for layer,points in route.paths]
        planned,joined = merge_fanins(board,route.net,planned,scene)
        merged += joined
        cache = {}
        for layer in (0, 1):
            for path in planned:
                if path.layer != layer:
                    continue
                points = path.points
                key = layer,path.width
                if key not in cache:
                    cache[key] = scene.obstacles(route.net,layer,radius=path.width/2),scene.allowed(route.net,path.width/2)
                forbidden,allowed = cache[key]
                original = LineString(points)
                if not Scene.legal(original, forbidden, allowed):
                    # A native-valid seed can sit on the conservative EPS margin.
                    # Preserve it and expose this in the report, never move blindly.
                    kept += 1
                    result.append((layer, points))
                    generated.extend(Primitive(route.net, layer, path.width, a, b)
                                     for a, b in zip(points, points[1:]))
                    continue
                if path.fixed:
                    spread = points
                else:
                    first,last = (1 if path.lead_start else 0),(-1 if path.lead_end else len(points))
                    middle = points[first:last]
                    simple = simplify(middle,forbidden,allowed) if len(middle)>1 else middle
                    spread = relax(simple,forbidden,allowed,options) if len(simple)>1 else simple
                    spread = (points[:1] if path.lead_start else [])+spread+(points[-1:] if path.lead_end else [])
                if spread != points:
                    changed += 1
                result.append((layer, spread))
                generated.extend(smooth_path(spread, route.net, layer, path.width,
                                             forbidden, allowed, options.radius,
                                             preserve_start=path.lead_start,preserve_end=path.lead_end))
        route.paths, route.primitives = result, generated
        if progress:
            progress(f"Shape {board.nets[route.net]}: {len(generated)} copper primitives")
    return ([p for r in routes for p in r.primitives], vias,
            dict(topological_paths=len(paths), spread_paths=changed, preserved_margin_paths=kept,
                 merged_fanins=merged, removed_single_layer_vias=original_vias-len(vias),
                 options=vars(replace(options))))
