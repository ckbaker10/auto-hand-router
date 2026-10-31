"""Deterministic two-layer A*, clearance-aware relaxation and native arc fillets."""

from dataclasses import dataclass
import heapq
import math

import numpy as np
from scipy.ndimage import distance_transform_edt
import shapely
from shapely.geometry import GeometryCollection, LineString, Point
from shapely.ops import nearest_points, unary_union

from .model import EPS, Primitive, Route, Via


class RoutingFailure(RuntimeError):
    def __init__(self, message, net=None):
        super().__init__(message)
        self.net = net


@dataclass(frozen=True)
class Options:
    grid: float = 0.20
    spacing: float = 1.2  # additional preferred margin beyond mandatory clearance
    spacing_weight: float = 1.7
    via_cost: float = 4.0
    bend_cost: float = 0.08
    radius: float = 1.0
    relax_steps: int = 30
    max_expansions: int = 450_000


class Scene:
    def __init__(self, board, routes=None):
        self.board = board
        self.routes = routes if routes is not None else []

    def obstacles(self, net, layer, radius=None, via=False, omit=None):
        rules = self.board.rules[net]
        half = rules.width / 2 if radius is None else radius
        shapes = []
        for area in self.board.rule_areas:
            if layer in area.layers and (area.vias if via else area.tracks):
                shapes.append(area.shape.buffer(half + EPS))
        if via:
            shapes.extend(region.buffer(half + rules.clearance + EPS)
                          for _, region in self.board.via_keepouts)
        for pad in self.board.pads:
            if layer not in pad.layers:
                continue
            if pad.net != net or not pad.plated:
                if pad.plated:
                    other = self.board.rules.get(pad.net, rules)
                    shapes.append(pad.shape.buffer(half + max(rules.clearance, other.clearance) + EPS))
                if pad.drill:
                    shapes.append(pad.hole().buffer(half + rules.hole + EPS))
            if via and pad.drill:
                shapes.append(pad.hole().buffer(rules.via_drill / 2 + rules.hole_to_hole + EPS))
            if via and pad.plated:
                # Same-net pad copper is also excluded: this PoC never uses via-in-pad.
                shapes.append(pad.shape.buffer(half + rules.clearance + EPS))
        for route in self.routes:
            if route is omit:
                continue
            if route.net != net:
                other = self.board.rules[route.net]
                clearance = max(rules.clearance, other.clearance)
                for route_layer, line in route.lines():
                    if route_layer == layer:
                        shapes.append(line.buffer(half + other.width / 2 + clearance + EPS, quad_segs=12))
                for v in route.vias:
                    shapes.append(Point(v.center).buffer(half + v.diameter / 2 + clearance + EPS))
            if via:
                for v in route.vias:
                    shapes.append(Point(v.center).buffer(
                        v.drill / 2 + rules.via_drill / 2 + rules.hole_to_hole + EPS))
        return unary_union(shapes) if shapes else GeometryCollection()

    def allowed(self, net, radius=None):
        r = self.board.rules[net]
        return self.board.boundary.buffer(-(r.edge + (r.width / 2 if radius is None else radius) + EPS))

    @staticmethod
    def legal(line, forbidden, allowed):
        return allowed.covers(line) and (forbidden.is_empty or line.disjoint(forbidden))

    def route_legal(self, route, omit=None):
        for layer, line in route.lines():
            if not self.legal(line, self.obstacles(route.net, layer, omit=omit), self.allowed(route.net)):
                return False
        return True


class GridSearch:
    DIRECTIONS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

    def __init__(self, scene, net, options):
        self.scene, self.net, self.options = scene, net, options
        self.pitch = options.grid
        minx, miny, maxx, maxy = scene.board.boundary.bounds
        self.origin = (math.floor(minx / self.pitch) * self.pitch, math.floor(miny / self.pitch) * self.pitch)
        self.nx = math.ceil((maxx - self.origin[0]) / self.pitch) + 1
        self.ny = math.ceil((maxy - self.origin[1]) / self.pitch) + 1
        self.x, self.y = np.meshgrid(self.origin[0] + np.arange(self.nx) * self.pitch,
                                      self.origin[1] + np.arange(self.ny) * self.pitch)
        self.allowed = scene.allowed(net)
        inside = shapely.contains_xy(self.allowed, self.x, self.y)
        self.forbidden = [scene.obstacles(net, layer) for layer in (0, 1)]
        self.free = [inside & ~shapely.intersects_xy(ob, self.x, self.y) for ob in self.forbidden]
        self.dist = [distance_transform_edt(f) * self.pitch for f in self.free]
        r = scene.board.rules[net]
        via_inside = shapely.contains_xy(scene.allowed(net, r.via_diameter / 2), self.x, self.y)
        self.via_free = via_inside.copy()
        for layer in (0, 1):
            ob = scene.obstacles(net, layer, radius=r.via_diameter / 2, via=True)
            self.via_free &= ~shapely.intersects_xy(ob, self.x, self.y)

    def point(self, x, y):
        return (round(self.origin[0] + x * self.pitch, 6), round(self.origin[1] + y * self.pitch, 6))

    def index(self, point):
        return (round((point[0] - self.origin[0]) / self.pitch),
                round((point[1] - self.origin[1]) / self.pitch))

    def find(self, source, target):
        sx, sy = self.index(source.center)
        gx, gy = self.index(target.center)
        goals = {(gx, gy, layer) for layer in target.layers
                 if self.free[layer][gy, gx] and Scene.legal(
                     LineString([self.point(gx, gy), target.center]), self.forbidden[layer], self.allowed)}
        if not goals:
            raise RoutingFailure(f"No legal grid access to {target.ref}.{target.number}")
        queue, best, previous = [], {}, {}
        for layer in source.layers:
            if self.free[layer][sy, sx] and Scene.legal(
                    LineString([source.center, self.point(sx, sy)]), self.forbidden[layer], self.allowed):
                state = (sx, sy, layer, 8)
                best[state] = 0.0
                heapq.heappush(queue, (self.pitch * math.hypot(sx - gx, sy - gy), 0.0, state))
        expansions, end = 0, None
        edge_cache = {}
        while queue:
            _, cost, state = heapq.heappop(queue)
            if cost != best.get(state):
                continue
            x, y, layer, direction = state
            if (x, y, layer) in goals:
                end = state
                break
            expansions += 1
            if expansions > self.options.max_expansions:
                raise RoutingFailure("Search expansion limit reached")
            for nd, (dx, dy) in enumerate(self.DIRECTIONS):
                xx, yy = x + dx, y + dy
                if not (0 <= xx < self.nx and 0 <= yy < self.ny and self.free[layer][yy, xx]):
                    continue
                if dx and dy and not (self.free[layer][y, xx] and self.free[layer][yy, x]):
                    continue
                edge = (layer, min((x, y), (xx, yy)), max((x, y), (xx, yy)))
                if edge not in edge_cache:
                    line = LineString([self.point(x, y), self.point(xx, yy)])
                    edge_cache[edge] = Scene.legal(line, self.forbidden[layer], self.allowed)
                if not edge_cache[edge]:
                    continue
                step = self.pitch * math.hypot(dx, dy)
                proximity = 0.0
                if self.options.spacing > 0:
                    proximity = self.options.spacing_weight * math.exp(-self.dist[layer][yy, xx] / self.options.spacing)
                turn = 0 if direction == 8 else min(abs(nd - direction), 8 - abs(nd - direction)) * self.options.bend_cost
                new_cost = cost + step * (1 + proximity) + turn
                nxt = (xx, yy, layer, nd)
                if new_cost + 1e-10 < best.get(nxt, math.inf):
                    best[nxt], previous[nxt] = new_cost, state
                    heuristic = self.pitch * math.hypot(xx - gx, yy - gy)
                    heapq.heappush(queue, (new_cost + heuristic, new_cost, nxt))
            if self.via_free[y, x]:
                nxt = (x, y, 1 - layer, 8)
                new_cost = cost + self.options.via_cost
                if new_cost + 1e-10 < best.get(nxt, math.inf):
                    best[nxt], previous[nxt] = new_cost, state
                    heapq.heappush(queue, (new_cost + self.pitch * math.hypot(x - gx, y - gy), new_cost, nxt))
        if end is None:
            raise RoutingFailure(f"No path for {source.ref}.{source.number} -> {target.ref}.{target.number}")
        chain = [end]
        while chain[-1] in previous:
            chain.append(previous[chain[-1]])
        chain.reverse()
        paths, vias = [], []
        layer = chain[0][2]
        points = [source.center, self.point(*chain[0][:2])]
        rules = self.scene.board.rules[self.net]
        for state in chain[1:]:
            p = self.point(*state[:2])
            if state[2] != layer:
                paths.append((layer, deduplicate(points)))
                vias.append(Via(self.net, p, rules.via_diameter, rules.via_drill))
                layer, points = state[2], [p]
            else:
                points.append(p)
        points.append(target.center)
        paths.append((layer, deduplicate(points)))
        paths = [(layer, points) for layer, points in paths if len(points) >= 2]
        return Route(self.net, paths, vias), expansions


def deduplicate(points):
    result = []
    for p in points:
        p = tuple(round(float(c), 6) for c in p)
        if not result or math.dist(result[-1], p) > 1e-8:
            result.append(p)
    return result


def simplify(points, forbidden, allowed, tolerance=0.08):
    raw = LineString(points)
    candidate = list(raw.simplify(tolerance, preserve_topology=False).coords)
    if Scene.legal(LineString(candidate), forbidden, allowed):
        return candidate
    # Greedy visibility shortening, each segment is tested against actual geometry.
    out, i = [points[0]], 0
    while i < len(points) - 1:
        j = len(points) - 1
        while j > i + 1 and not Scene.legal(LineString([points[i], points[j]]), forbidden, allowed):
            j -= 1
        out.append(points[j])
        i = j
    return out


def relax(points, forbidden, allowed, options):
    if options.spacing <= 0 or options.relax_steps <= 0:
        return points
    line = LineString(points)
    count = max(len(points), math.ceil(line.length / 0.85) + 1)
    coords = np.array([line.interpolate(t, normalized=True).coords[0] for t in np.linspace(0, 1, count)])
    # Preserve sharp obstacle corners when resampling would shortcut through copper.
    if not Scene.legal(LineString(coords), forbidden, allowed):
        coords = np.array(points, dtype=float)
    obstacles = unary_union([forbidden, allowed.boundary])
    for _ in range(options.relax_steps):
        if len(coords) < 3:
            break
        delta = np.zeros_like(coords)
        for i in range(1, len(coords) - 1):
            p = Point(coords[i])
            q = np.array(nearest_points(p, obstacles)[1].coords[0])
            vector = coords[i] - q
            distance = float(np.linalg.norm(vector))
            repel = vector / max(distance, 1e-9) * max(0, 1 - distance / options.spacing)
            smooth = (coords[i - 1] + coords[i + 1]) / 2 - coords[i]
            delta[i] = 0.20 * smooth + 0.10 * repel
        moved = False
        for factor in (1, 0.5, 0.25, 0.125):
            candidate = coords + factor * delta
            candidate_line = LineString(candidate)
            if candidate_line.length <= line.length * 1.20 and Scene.legal(candidate_line, forbidden, allowed):
                coords = candidate
                moved = True
                break
        if not moved or np.max(np.linalg.norm(delta, axis=1)) < 0.002:
            break
    return simplify(deduplicate(coords), forbidden, allowed, tolerance=0.04)


def fillet(points, net, layer, width, forbidden, allowed, radius, angle_threshold=0.02):
    result = []
    anchor = tuple(points[0])
    for i in range(1, len(points) - 1):
        p, corner, q = map(np.array, (points[i - 1], points[i], points[i + 1]))
        before, after = corner - p, q - corner
        a, b = np.linalg.norm(before), np.linalg.norm(after)
        if min(a, b) < 1e-7:
            continue
        u, v = before / a, after / b
        angle = math.acos(float(np.clip(u @ v, -1, 1)))
        cross = u[0] * v[1] - u[1] * v[0]
        arc = None
        if angle_threshold < angle < math.pi - 0.03 and abs(cross) > 1e-8:
            cut = min(radius * math.tan(angle / 2), a * 0.45, b * 0.45)
            for _ in range(8):
                r = cut / math.tan(angle / 2)
                start, end = corner - u * cut, corner + v * cut
                center = start + np.array([-u[1], u[0]]) * math.copysign(r, cross)
                theta = math.atan2(*(start - center)[::-1])
                middle = center + r * np.array([math.cos(theta + math.copysign(angle / 2, cross)),
                                                math.sin(theta + math.copysign(angle / 2, cross))])
                candidate = Primitive(net, layer, width, tuple(start), tuple(end), tuple(middle))
                curve = LineString([anchor] + candidate.points())
                if Scene.legal(curve, forbidden, allowed):
                    arc = candidate
                    break
                cut *= 0.5
                if r < 0.04:
                    break
        if arc:
            if math.dist(anchor, arc.start) > 1e-7:
                result.append(Primitive(net, layer, width, anchor, arc.start))
            result.append(arc)
            anchor = arc.end
        else:
            point = tuple(corner)
            if math.dist(anchor, point) > 1e-7:
                result.append(Primitive(net, layer, width, anchor, point))
            anchor = point
    if math.dist(anchor, points[-1]) > 1e-7:
        result.append(Primitive(net, layer, width, anchor, tuple(points[-1])))
    return result


def _route_board(board, options, net_order, progress=None):
    if not (0.05 <= options.grid <= 0.5) or options.spacing < 0:
        raise ValueError("Grid must be 0.05–0.5 mm and spacing nonnegative")
    pads_by_net = {}
    for pad in board.pads:
        if pad.net and pad.plated:
            pads_by_net.setdefault(pad.net, []).append(pad)
    scene = Scene(board)
    searches = []
    # Low-pin-count nets first; the larger supply trees can exploit the second layer.
    nets = net_order
    for net in nets:
        pads = sorted(pads_by_net[net], key=lambda p: (p.center, p.ref, p.number))
        connected, pending = [pads[0]], pads[1:]
        while pending:
            _, source, target = min((math.dist(s.center, t.center), i, j)
                                    for i, s in enumerate(connected) for j, t in enumerate(pending))
            start, goal = connected[source], pending[target]
            search = GridSearch(scene, net, options)
            try:
                route, expanded = search.find(start, goal)
            except RoutingFailure as exc:
                raise RoutingFailure(str(exc), net=net) from exc
            route.paths = [(layer, simplify(points, search.forbidden[layer], search.allowed))
                           for layer, points in route.paths]
            scene.routes.append(route)
            connected.append(pending.pop(target))
            searches.append(expanded)
            if progress:
                progress(f"{board.nets[net]}: {start.ref}.{start.number} -> {goal.ref}.{goal.number}; {expanded} states")
    # Shape relaxation is always constrained by the other nets' full current copper.
    for route in scene.routes:
        paths = []
        for layer, points in route.paths:
            forbidden = scene.obstacles(route.net, layer)
            paths.append((layer, relax(points, forbidden, scene.allowed(route.net), options)))
        route.paths = paths
    for route in scene.routes:
        primitives = []
        for layer, points in route.paths:
            forbidden = scene.obstacles(route.net, layer)
            primitives.extend(fillet(points, route.net, layer, board.rules[route.net].width,
                                     forbidden, scene.allowed(route.net), options.radius))
        route.primitives = primitives
        if not scene.route_legal(route):
            raise RoutingFailure("Final rounded geometry violated the conservative clearance model")
    return scene.routes, {"connections_routed": len(scene.routes), "expanded_states": sum(searches),
                          "nets": len(pads_by_net), "net_order": nets, "options": vars(options)}


def route_board(board, options=Options(), progress=None):
    """Bounded whole-plan retries prioritize a net blocked by earlier choices.

    A failed speculative plan is discarded completely; only a complete legal
    plan is returned. There is no claim of completeness or global optimality.
    """
    counts = {}
    for pad in board.pads:
        if pad.net and pad.plated:
            counts[pad.net] = counts.get(pad.net, 0) + 1
    order = sorted(counts, key=lambda net: (counts[net], net))
    failures = []
    tried = set()
    for _ in range(4):
        if tuple(order) in tried:
            break
        tried.add(tuple(order))
        try:
            routes, stats = _route_board(board, options, order, progress)
            stats["routing_attempts"] = len(tried)
            stats["discarded_attempts"] = failures
            return routes, stats
        except RoutingFailure as exc:
            failures.append(str(exc))
            if exc.net is None:
                raise
            if progress:
                progress(f"Retrying complete plan with {board.nets[exc.net]} first")
            order = [exc.net] + [net for net in order if net != exc.net]
    raise RoutingFailure("Routing failed after bounded net-order retries: " + "; ".join(failures))
