"""Tangent pad/via shoulders following the finished copper, including native arcs.

Inspired by mitxela's Melting KiCad articles; independent implementation.
Keep centerlines intact. Emit only complete, clearance-checked shoulders; a tight
escape gets a shorter shoulder or stays unchanged rather than a clipped cusp.
"""
from collections import defaultdict
from dataclasses import dataclass
import math

import numpy as np
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .model import EPS, LAYERS, Route, arc_geometry
from .router import Scene
from .sexpr import child, children

ZONE_NAME = "auto-hand-router:teardrop"


@dataclass(frozen=True)
class TeardropOptions:
    length_ratio: float = 1.0
    max_length: float = 3.0
    size: float = .875

    def __post_init__(self):
        if not all(math.isfinite(v) and v > 0 for v in (self.length_ratio, self.max_length)):
            raise ValueError("Teardrop length ratio and maximum length must be positive and finite")
        if not math.isfinite(self.size) or not 0 < self.size <= 1:
            raise ValueError("Teardrop size must be greater than zero and at most one")


@dataclass
class Teardrop:
    net: int
    layer: int
    shape: object


def key(point):
    return tuple(round(float(v), 6) for v in point)


def bezier(a, b, c, d, tolerance=.001):
    """Adaptive polygonal boundary, with a control-hull error bound in mm."""
    a, b, c, d = (np.asarray(p, dtype=float) for p in (a, b, c, d))
    def sample(a, b, c, d, depth):
        chord = LineString([a, d])
        if max(Point(b).distance(chord), Point(c).distance(chord)) <= tolerance:
            return [tuple(a), tuple(d)]
        if depth >= 16:
            raise ValueError("Teardrop boundary did not converge")
        ab, bc, cd = (a+b)/2, (b+c)/2, (c+d)/2
        abc, bcd = (ab+bc)/2, (bc+cd)/2
        middle = (abc+bcd)/2
        return sample(a, ab, abc, middle, depth+1)[:-1] + sample(middle, bcd, cd, d, depth+1)
    return sample(a, b, c, d, 0)


def round_contact(shape, center):
    coords = np.asarray(shape.exterior.coords[:-1])
    radii = np.linalg.norm(coords-np.asarray(center),axis=1)
    # Rectangle corners are also equidistant from their center. Require a
    # tessellated contour and circular area, not just equal vertex radii.
    return (len(coords) >= 16 and min(radii) > 0 and max(radii)-min(radii) < .002
            and shape.area/(math.pi*float(np.mean(radii))**2) > .99)


def anchors(shape, center, direction, size=1.):
    """Support points and outward tangents on each flank of a convex contact.

    Circular contacts use analytic tangents. Long straight sides use their
    midpoint, avoiding attaching a smooth shoulder at a rectangular corner.
    Other convex outlines use their short tessellated boundary edge.
    """
    center, direction = np.asarray(center), np.asarray(direction)
    normal = np.array([-direction[1], direction[0]])
    coords = np.asarray(shape.exterior.coords[:-1])
    radii = np.linalg.norm(coords-center, axis=1)
    if round_contact(shape,center):
        radius = float(np.mean(radii))
        advance = math.sqrt(max(0.,1-size*size))
        return [(center+radius*(advance*direction+side*size*normal),
                 size*direction-side*advance*normal) for side in (1, -1)]
    result = []
    for side in (1, -1):
        if size < 1.:
            # Move the attachment along the actual pad boundary. Scaling an
            # existing polygon would detach it or lose its boundary tangent.
            transverse = (coords-center) @ normal
            offset = (max(transverse) if side == 1 else min(transverse))*size
            origin = center+normal*offset
            reach = max(radii)*4
            chord = shape.intersection(LineString([origin-direction*reach,origin+direction*reach]))
            if chord.is_empty or chord.geom_type != "LineString":
                raise ValueError("Unsupported teardrop contact contour")
            position = max((np.array(p) for p in chord.coords),key=lambda p:float((p-center) @ direction))
            edges = [(coords[i],coords[(i+1)%len(coords)]) for i in range(len(coords))]
            a,b = min(edges,key=lambda edge:Point(position).distance(LineString(edge)))
            tangent = b-a
            tangent /= np.linalg.norm(tangent)
            if tangent @ (direction-side*normal) < 0:
                tangent = -tangent
            result.append((position,tangent))
            continue
        support = int(np.argmax((coords-center) @ (normal*side)))
        choices = []
        for other in ((support-1) % len(coords), (support+1) % len(coords)):
            vector = coords[other]-coords[support]
            length = np.linalg.norm(vector)
            if length < 1e-8:
                continue
            tangent = vector/length
            if tangent @ direction < 0:
                tangent = -tangent
            choices.append((float(tangent @ direction), (coords[other]+coords[support])/2, tangent))
        _, position, tangent = max(choices, key=lambda v:v[0])
        result.append((position, tangent))
    return result


def shoulder(shape, center, tail, tangent, width, size=1.):
    center, tail, tangent = map(np.asarray, (center, tail, tangent))
    delta = tail-center
    direction = delta/np.linalg.norm(delta)
    normal = np.array([-tangent[1], tangent[0]])
    sides = []
    for side, (anchor, lead) in zip((1, -1), anchors(shape, center, direction,size)):
        end = tail + side*normal*width/2
        # Each flank gets its own length: offset entries are asymmetric.
        distance = np.linalg.norm(end-anchor)
        outward = np.array([-lead[1],lead[0]])
        if outward @ (anchor-center) < 0:
            outward = -outward
        depth = float((end-anchor) @ outward)
        circular = round_contact(shape,center)
        back = distance*.40
        projection = float(tangent @ outward)
        if not circular and size < 1.:
            if depth < -1e-6:
                return None
            if projection > 1e-9:
                back = min(back,max(0.,depth)*.9/projection)
        forward = distance*.45
        if not circular:
            # A straight-side tangent must not overshoot the next pad corner.
            ray = LineString([anchor,anchor+lead*forward])
            inside = ray.intersection(shape.buffer(1e-7))
            available = 0.
            for part in getattr(inside,"geoms",[inside]):
                if part.geom_type == "LineString" and Point(anchor).distance(part) < 1e-6:
                    available = max(available,max(float((np.array(p)-anchor) @ lead) for p in part.coords))
            forward = min(forward,available*.9)
        sides.append(bezier(anchor, anchor+lead*forward, end-tangent*back, end))
    # An inner cap gives front-edge rectangle anchors positive copper overlap,
    # rather than relying on a zero-area contact along the pad boundary.
    polygon = Polygon(sides[0]+sides[1][::-1]+[tuple(center)])
    if not polygon.is_valid or polygon.is_empty or polygon.area < 1e-5:
        return None
    return polygon


def sample_tail(pieces, primitives, distance):
    for index, line in pieces:
        if distance > line.length:
            distance -= line.length
            continue
        point = np.array(line.interpolate(distance).coords[0])
        primitive = primitives[index]
        direction = np.array(line.coords[-1])-line.coords[0]
        if primitive.mid is None:
            tangent = np.array(primitive.end)-primitive.start
        else:
            center, radius, _, sweep = arc_geometry(primitive.start, primitive.mid, primitive.end)
            radial = point-center
            radial = radial/np.linalg.norm(radial)
            point = center+radius*radial
            tangent = np.array([-radial[1], radial[0]])*math.copysign(1, sweep)
        if tangent @ direction < 0:
            tangent = -tangent
        return point, tangent/np.linalg.norm(tangent), primitive.width
    raise ValueError("Teardrop extends beyond its connected track")


def acute_tips(shape):
    """Locate narrow outward copper tips after union, including overlapping shoulders.

    Use KiCad 10's default sliver angle/width as a conservative local precheck.
    Native DRC remains authoritative, with its own arc/polygon approximations.
    """
    result = []
    for polygon in getattr(shape, "geoms", [shape]):
        if polygon.geom_type != "Polygon":
            continue
        points = np.asarray(polygon.exterior.coords[:-1])
        if len(points) < 3:
            continue
        before = np.roll(points,1,axis=0)-points
        after = np.roll(points,-1,axis=0)-points
        product = np.linalg.norm(before,axis=1)*np.linalg.norm(after,axis=1)
        cosine = (before*after).sum(axis=1)/np.maximum(product,1e-20)
        cross = before[:,0]*after[:,1]-before[:,1]*after[:,0]
        convex = cross*(1 if polygon.exterior.is_ccw else -1) < 0
        narrow = (cosine > math.cos(math.radians(20))) & (cosine < 1-1e-8)
        wide = np.linalg.norm(after-before,axis=1) > .08
        result.extend(Point(p) for p in points[convex & narrow & wide])
    return result


def generate(board, primitives, vias, options=None):
    options = options or TeardropOptions()
    primitives = list(primitives)
    graph = defaultdict(list)
    for i, p in enumerate(primitives):
        graph[p.net, p.layer, key(p.start)].append(i)
        graph[p.net, p.layer, key(p.end)].append(i)
    contacts = [(p.net, layer, p.center, p.shape) for p in board.pads if p.plated and p.net
                for layer in p.layers]
    contacts += [(v.net, layer, v.center, Point(v.center).buffer(v.diameter/2, quad_segs=64))
                 for v in vias if v.net for layer in (0, 1)]
    contacts.sort(key=lambda c:(c[1], c[2], c[0]))
    scene = Scene(board, [Route(net, [], [v for v in vias if v.net == net],
                               [p for p in primitives if p.net == net])
                          for net in sorted({p.net for p in primitives})])
    by_net_layer = defaultdict(list)
    lines = [p.line() for p in primitives]
    for i, p in enumerate(primitives):
        by_net_layer[p.net, p.layer].append(i)
    trees = {group:STRtree([lines[i] for i in indices]) for group,indices in by_net_layer.items()}
    copper_cache = {}
    contact_shapes = defaultdict(list)
    for n, lay, _, shape in contacts:
        contact_shapes[n, lay].append(shape)
    contact_trees = {group:STRtree(shapes) for group,shapes in contact_shapes.items()}
    result, cache, seen = [], {}, set()
    candidates, shortened, skipped = 0, 0, 0
    for net, layer, center, shape in contacts:
        diameter = min(shape.bounds[2]-shape.bounds[0], shape.bounds[3]-shape.bounds[1])
        if diameter <= 0:
            continue
        group = net, layer
        if group not in trees:
            continue
        nearby = sorted(by_net_layer[group][int(i)] for i in trees[group].query(shape, predicate="intersects"))
        for index in nearby:
            p = primitives[index]
            if p.width >= diameter*.9:
                continue
            outside = lines[index].difference(shape)
            for part in getattr(outside, "geoms", [outside]):
                if part.geom_type != "LineString" or part.is_empty or part.length < 1e-6:
                    continue
                for reverse in (False, True):
                    coords = list(part.coords)[::-1] if reverse else list(part.coords)
                    if Point(coords[0]).distance(shape.boundary) > 2e-5:
                        continue
                    node = key(coords[-1])
                    if node not in (key(p.start), key(p.end)):
                        continue
                    identity = (net, layer, key(center), key(coords[0]))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    candidates += 1
                    pieces, used = [(index, LineString(coords))], {index}
                    target = min(diameter*options.length_ratio, options.max_length)
                    length = pieces[0][1].length
                    while length < target:
                        neighbours = graph[net, layer, node]
                        # Stop at a branch, width change or cycle; never pick an arm.
                        if len(neighbours) != 2:
                            break
                        choices = [i for i in neighbours if i not in used]
                        if len(choices) != 1:
                            break
                        nxt = choices[0]
                        other = primitives[nxt]
                        if abs(other.width-p.width) > 1e-6:
                            break
                        points = other.points()
                        if key(points[0]) != node:
                            points.reverse()
                        line = LineString(points)
                        if line.length < 1e-8:
                            break
                        pieces.append((nxt, line))
                        length += line.length
                        used.add(nxt)
                        node = key(points[-1])
                    if length < target:
                        target = length*.95
                    target *= options.size
                    if target < p.width*.6:
                        skipped += 1
                        continue
                    if (net, layer) not in cache:
                        cache[net, layer] = scene.obstacles(net, layer, radius=0), scene.allowed(net, radius=0)
                    forbidden, allowed = cache[net, layer]
                    foreign = [t.shape.buffer(max(board.rules[net].clearance, board.rules[t.net].clearance)+EPS)
                               for t in result if t.layer == layer and t.net != net]
                    obstacles = unary_union([forbidden, *foreign]) if foreign else forbidden
                    accepted = False
                    for scale in (1., .75, .5, .3):
                        distance = target*scale
                        if distance < p.width*.6:
                            continue
                        tail, tangent, width = sample_tail(pieces, primitives, distance)
                        polygon = shoulder(shape, center, tail, tangent, width,options.size)
                        if polygon is None or not Scene.legal(polygon, obstacles, allowed):
                            continue
                        # Actual copper must overlap both the contact and its lead.
                        if polygon.intersection(shape).area < 1e-5:
                            continue
                        # A legal zone in isolation can form a sharp copper tip
                        # where it meets another shoulder or its curved lead.
                        margin = max(primitives[i].width for i in by_net_layer[group])/2+.1
                        ids = trees[group].query(polygon.envelope,predicate="dwithin",distance=margin)
                        original = []
                        for j in ids:
                            k = by_net_layer[group][int(j)]
                            if k not in copper_cache:
                                copper_cache[k] = lines[k].buffer(primitives[k].width/2,quad_segs=24)
                            original.append(copper_cache[k])
                        cids = contact_trees[group].query(polygon.envelope,predicate="intersects")
                        original.extend(contact_shapes[group][int(j)] for j in cids)
                        original.extend(t.shape for t in result if t.net == net and t.layer == layer
                                        and t.shape.intersects(polygon.envelope))
                        base = unary_union(original)
                        existing_tips = acute_tips(base)
                        if any(not any(tip.distance(old) < .01 for old in existing_tips)
                               for tip in acute_tips(base.union(polygon))):
                            continue
                        result.append(Teardrop(net, layer, polygon))
                        shortened += scale < 1.
                        accepted = True
                        break
                    skipped += not accepted
    shoulders = len(result)
    merged = []
    for net, layer in sorted({(t.net,t.layer) for t in result}):
        shape = unary_union([t.shape for t in result if t.net == net and t.layer == layer])
        for polygon in sorted(getattr(shape,"geoms",[shape]),key=lambda p:p.bounds):
            merged.append(Teardrop(net,layer,polygon))
    # Overlapping same-net shoulders must be one zone. Separate priorities clip
    # their near-tangent borders into tiny slivers; equal priorities produce
    # native zone-intersection findings. A union preserves the intended copper.
    return merged, dict(candidates=candidates, generated=len(merged), shoulders=shoulders, shortened=shortened,
                        skipped=skipped, length_ratio=options.length_ratio, max_length_mm=options.max_length,
                        size=options.size)


def read_teardrops(root, nets):
    """Read our zone copper; use native filled contours when present."""
    ids = {name:net for net, name in nets.items()}
    result = []
    for zone in children(root, "zone"):
        if child(zone, "name") != ["name", ZONE_NAME]:
            continue
        token = child(zone, "net")[1]
        net = ids[token] if not children(root, "net") else int(token)
        layer = LAYERS.index(child(zone, "layer")[1])
        filled = children(zone, "filled_polygon")
        polygons = filled or children(zone, "polygon")
        rings = [[tuple(map(float, x[1:3])) for x in children(child(p, "pts"), "xy")] for p in polygons]
        shape = unary_union([Polygon(r) for r in rings]) if filled else Polygon(rings[0],rings[1:])
        result.append(Teardrop(net, layer, shape))
    return result
