"""Supported KiCad geometry and conservative two-layer project rules (millimetres)."""

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union

from .sexpr import child, children, parse, xy

LAYERS = ("F.Cu", "B.Cu")
EPS = 0.012  # conservative budget for tessellation and export quantisation
ARC_ERROR = 0.0005


class UnsupportedBoard(ValueError):
    pass


def matches_netclass(name, pattern):
    """KiCad's netclass context accepts anchored regex OR anchored wildcards.

    In particular /*GND is also the regex 'zero or more slashes, then GND',
    so it matches the root GND net. A shell-style glob alone misses this.
    Advanced POSIX classes and buses are outside this reference subset.
    """
    if "[:" in pattern or "(?" in pattern or "{" in pattern or "[" in pattern:
        raise UnsupportedBoard("Advanced netclass pattern syntax is outside this PoC's scope")
    wildcard = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    try:
        regex_matches = re.fullmatch(pattern, name) is not None
    except re.error:
        regex_matches = False
    return regex_matches or re.fullmatch(wildcard, name) is not None


@dataclass(frozen=True)
class Rules:
    width: float = 0.381
    clearance: float = 0.127
    via_diameter: float = 0.889
    via_drill: float = 0.381
    edge: float = 0.25
    hole: float = 0.25
    hole_to_hole: float = 0.5


@dataclass
class Pad:
    ref: str
    number: str
    net: int
    center: tuple
    layers: tuple
    shape: object
    drill: float = 0.0
    plated: bool = True
    drill_shape: object = None

    def hole(self):
        return self.drill_shape if self.drill_shape is not None else Point(self.center).buffer(self.drill / 2, quad_segs=48)


@dataclass
class RuleArea:
    shape: object
    layers: tuple
    tracks: bool
    vias: bool


@dataclass
class Primitive:
    net: int
    layer: int
    width: float
    start: tuple
    end: tuple
    mid: tuple | None = None

    def points(self):
        if self.mid is None:
            return [self.start, self.end]
        return arc_points(self.start, self.mid, self.end)

    def line(self):
        return LineString(self.points())


@dataclass
class Via:
    net: int
    center: tuple
    diameter: float
    drill: float


@dataclass
class Route:
    net: int
    paths: list  # [(layer, [xy, ...]), ...]
    vias: list = field(default_factory=list)
    primitives: list = field(default_factory=list)

    def lines(self):
        if self.primitives:
            return [(p.layer, p.line()) for p in self.primitives]
        return [(layer, LineString(points)) for layer, points in self.paths if len(points) >= 2]


def arc_geometry(start, mid, end):
    a, b, c = np.array(start), np.array(mid), np.array(end)
    matrix = 2 * np.array([b - a, c - a])
    if abs(np.linalg.det(matrix)) < 1e-12:
        raise UnsupportedBoard("Degenerate arc")
    center = np.linalg.solve(matrix, np.array([b @ b - a @ a, c @ c - a @ a]))
    radius = float(np.linalg.norm(a - center))
    angles = [math.atan2(*(p - center)[::-1]) for p in (a, b, c)]
    sweep = (angles[2] - angles[0]) % math.tau
    if (angles[1] - angles[0]) % math.tau > sweep + 1e-10:
        sweep -= math.tau
    return center, radius, angles[0], sweep


def arc_points(start, mid, end, error=ARC_ERROR):
    center, radius, angle, sweep = arc_geometry(start, mid, end)
    step = 2 * math.acos(max(-1, min(1, 1 - error / radius)))
    count = max(2, math.ceil(abs(sweep) / max(step, 1e-6)))
    angles = np.linspace(angle, angle + sweep, count + 1)
    points = center + radius * np.column_stack((np.cos(angles), np.sin(angles)))
    points[0], points[-1] = start, end
    return [tuple(p) for p in points]


def rotate_local(point, origin, degrees):
    angle = math.radians(-degrees)  # KiCad stores CCW angles in screen coordinates
    x, y = point
    return (origin[0] + x * math.cos(angle) - y * math.sin(angle),
            origin[1] + x * math.sin(angle) + y * math.cos(angle))


def pad_shape(kind, size, ratio=0):
    w, h = size
    if kind == "circle":
        if abs(w - h) > 1e-8:
            raise UnsupportedBoard("Noncircular circle pad")
        return Point(0, 0).buffer(w / 2, quad_segs=32)
    if kind == "rect":
        return box(-w / 2, -h / 2, w / 2, h / 2)
    if kind in ("oval", "roundrect"):
        r = min(w, h) / 2 if kind == "oval" else min(w, h) * ratio
        if not 0 <= r <= min(w, h) / 2:
            raise UnsupportedBoard("Invalid pad corner radius")
        if r < 1e-9:
            return pad_shape("rect", size)
        core = box(-w / 2 + r, -h / 2 + r, w / 2 - r, h / 2 - r)
        if kind == "oval":
            core = LineString([(-w / 2 + r, -h / 2 + r), (w / 2 - r, h / 2 - r)])
        return core.buffer(r, quad_segs=32)
    raise UnsupportedBoard(f"Unsupported pad shape {kind}")


@dataclass
class Board:
    source: Path
    project: Path
    text: str
    expression: object
    pads: list
    boundary: Polygon
    nets: dict
    rules: dict
    original_primitives: list
    original_vias: list
    via_keepouts: list = field(default_factory=list)  # [(reference, placement/courtyard region)]
    rule_areas: list = field(default_factory=list)
    copper_zones: list = field(default_factory=list)
    named_nets: bool = False
    display_regions: list = field(default_factory=list)

    @classmethod
    def load(cls, board_path, project_path=None):
        path = Path(board_path)
        project = Path(project_path) if project_path else path.with_suffix(".kicad_pro")
        if not project.is_file():
            raise UnsupportedBoard("A .kicad_pro project file is required")
        if path.with_suffix(".kicad_dru").exists():
            validate_custom_rules(path.with_suffix(".kicad_dru"))
        text = path.read_text()
        root = parse(text)
        if root[0] != "kicad_pcb":
            raise UnsupportedBoard("Not a KiCad board")
        layers = [n[1] for n in child(root, "layers")[1:]
                  if isinstance(n, list) and len(n) >= 3 and n[1].endswith(".Cu")]
        if tuple(layers) != LAYERS:
            raise UnsupportedBoard("This PoC supports exactly F.Cu and B.Cu")
        if children(root, "group"):
            raise UnsupportedBoard("Copper groups require explicit membership updates")
        nets = {int(n[1]): n[2] for n in children(root, "net")}
        named_nets = not nets
        if not nets:
            names = sorted({child(p, "net")[1] for fp in children(root, "footprint")
                            for p in children(fp, "pad") if child(p, "net")})
            nets = dict(enumerate([""] + [n for n in names if n]))
        net_ids = {name: net for net, name in nets.items()}
        data = json.loads(project.read_text())
        global_rules = data["board"]["design_settings"]["rules"]
        settings = data["net_settings"]
        classes = {c["name"]: c for c in settings["classes"]}
        assignments = settings.get("netclass_assignments") or {}
        patterns = settings.get("netclass_patterns") or []
        rules = {}
        for net, name in nets.items():
            names = assignments.get(name, [])
            names = [names] if isinstance(names, str) else names
            names = names + [p["netclass"] for p in patterns if matches_netclass(name, p["pattern"])]
            matched = [classes[n] for n in sorted(set(names))] or [classes["Default"]]
            if len(matched) != 1:
                raise UnsupportedBoard("Composite netclasses are not supported by the PoC")
            c = matched[0]
            rules[net] = Rules(
                width=max(float(c["track_width"]), global_rules["min_track_width"]),
                clearance=max(float(c["clearance"]), global_rules["min_clearance"]),
                via_diameter=max(float(c["via_diameter"]), global_rules["min_via_diameter"]),
                via_drill=max(float(c["via_drill"]), global_rules["min_through_hole_diameter"]),
                edge=global_rules["min_copper_edge_clearance"],
                hole=global_rules["min_hole_clearance"],
                hole_to_hole=global_rules["min_hole_to_hole"],
            )
        pads, via_keepouts = [], []
        for fp in children(root, "footprint"):
            if child(fp, "layer")[1] != "F.Cu":
                raise UnsupportedBoard("Back-side footprints are outside this PoC's scope")
            pos = xy(fp)
            fp_angle = float(child(fp, "at")[3]) if len(child(fp, "at")) > 3 else 0
            ref = next((p[2] for p in children(fp, "property") if p[1] == "Reference"), "?")
            fp_pad_start = len(pads)
            if children(fp, "zone"):
                raise UnsupportedBoard("Footprint-local rule areas are not supported")
            for graphic in fp:
                if isinstance(graphic, list) and graphic and graphic[0].startswith("fp_"):
                    layer = child(graphic, "layer")
                    if layer and layer[1] in LAYERS:
                        raise UnsupportedBoard("Footprint copper graphics are not supported")
            for p in children(fp, "pad"):
                if p[2] not in ("smd", "thru_hole", "np_thru_hole"):
                    raise UnsupportedBoard(f"Unsupported pad type {p[2]}")
                for key in ("clearance", "net_tie_pad_groups", "offset"):
                    if child(p, key) is not None:
                        raise UnsupportedBoard(f"Unsupported local pad override: {key}")
                center = rotate_local(xy(p), pos, fp_angle)
                at = child(p, "at")
                angle = float(at[3]) if len(at) > 3 else fp_angle
                ratio = child(p, "roundrect_rratio", [None, "0"])
                shape = pad_shape(p[3], xy(p, "size"), float(ratio[1]))
                shape = affinity.translate(affinity.rotate(shape, -angle, origin=(0, 0)), *center)
                drill_node = child(p, "drill")
                hole_shape = None
                if drill_node and child(drill_node, "offset"):
                    raise UnsupportedBoard("Offset drills are not supported")
                if drill_node and drill_node[1] == "oval":
                    size = tuple(map(float, drill_node[2:4]))
                    hole_shape = pad_shape("oval", size)
                    hole_shape = affinity.translate(affinity.rotate(hole_shape, -angle, origin=(0, 0)), *center)
                    drill = min(size)
                else:
                    drill = float(drill_node[1]) if drill_node else 0
                token = child(p, "net", [None, "" if named_nets else "0"])[1]
                net = net_ids[token] if named_nets else int(token)
                if net and net not in nets:
                    raise UnsupportedBoard("Pad refers to a missing net")
                pad_layers = child(p, "layers")[1:]
                copper = (0, 1) if "*.Cu" in pad_layers else tuple(i for i, name in enumerate(LAYERS) if name in pad_layers)
                pads.append(Pad(ref, p[1], net, center, copper, shape, drill, p[2] != "np_thru_hole", hole_shape))
            placement = footprint_region(fp, pos, fp_angle)
            pad_regions = [p.shape for p in pads[fp_pad_start:]]
            if placement is not None or pad_regions:
                region = unary_union(([placement] if placement is not None else []) + pad_regions)
                has_courtyard = any(isinstance(n, list) and n and
                                    child(n, "layer") == ["layer", "F.CrtYd"] for n in fp)
                if not has_courtyard:
                    region = region.convex_hull
                via_keepouts.append((ref, region))
        outline = []
        for n in root:
            if not isinstance(n, list) or not n:
                continue
            layer = child(n, "layer")
            if n[0].startswith("gr_") and layer and layer[1] in LAYERS:
                raise UnsupportedBoard("Board copper graphics are not supported")
            if layer != ["layer", "Edge.Cuts"]:
                continue
            if n[0] == "gr_line":
                outline.append(LineString([xy(n, "start"), xy(n, "end")]))
            elif n[0] == "gr_arc":
                outline.append(LineString(arc_points(xy(n, "start"), xy(n, "mid"), xy(n, "end"))))
            elif n[0] == "gr_rect":
                a, b = xy(n, "start"), xy(n, "end")
                outline.append(box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])).boundary)
            else:
                raise UnsupportedBoard("Only line/arc board contours are supported")
        polygons = list(polygonize(unary_union(outline)))
        if len(polygons) != 1 or not polygons[0].is_valid:
            raise UnsupportedBoard("Expected one closed board outline without cutouts")
        areas, zones = [], []
        for zone in children(root, "zone"):
            keepout = child(zone, "keepout")
            if keepout is None:
                zones.append(zone)
                continue
            layer_names = child(zone, "layers") or child(zone, "layer")
            layers = tuple(i for i, name in enumerate(LAYERS) if name in layer_names[1:] or "*.Cu" in layer_names[1:])
            for polygon in children(zone, "polygon"):
                region = Polygon([(float(q[1]), float(q[2])) for q in child(polygon, "pts")[1:]])
                if not region.is_valid:
                    raise UnsupportedBoard("Invalid rule area")
                areas.append(RuleArea(region, layers,
                                      child(keepout, "tracks", [None, "allowed"])[1] == "not_allowed",
                                      child(keepout, "vias", [None, "allowed"])[1] == "not_allowed"))
        primitives, vias = read_copper(root, nets)
        return cls(path, project, text, root, pads, polygons[0], nets, rules, primitives, vias,
                   via_keepouts, areas, zones, named_nets)


def footprint_region(footprint, origin, angle):
    """Closed F.CrtYd envelopes; fallback to F.Fab or silkscreen body geometry."""
    for layer_name in ("F.CrtYd", "F.Fab", "F.SilkS"):
        lines, areas = [], []
        for graphic in footprint:
            if not isinstance(graphic, list) or not graphic or child(graphic, "layer") != ["layer", layer_name]:
                continue
            kind = graphic[0]
            if kind == "fp_line":
                lines.append(LineString([xy(graphic, "start"), xy(graphic, "end")]))
            elif kind == "fp_rect":
                a, b = xy(graphic, "start"), xy(graphic, "end")
                areas.append(box(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])))
            elif kind == "fp_circle":
                center, end = xy(graphic, "center"), xy(graphic, "end")
                areas.append(Point(center).buffer(math.dist(center, end), quad_segs=32))
            elif kind == "fp_arc":
                lines.append(LineString(arc_points(xy(graphic, "start"), xy(graphic, "mid"), xy(graphic, "end"))))
            elif kind == "fp_poly":
                areas.append(Polygon([(float(n[1]), float(n[2]))
                                      for n in child(graphic, "pts")[1:] if n[0] == "xy"]))
        if not areas and not lines:
            continue
        areas += list(polygonize(unary_union(lines))) if lines else []
        if not areas:
            if layer_name == "F.CrtYd":
                raise UnsupportedBoard("An open courtyard cannot safely exclude vias")
            areas = [unary_union(lines).convex_hull]
        local = unary_union(areas)
        return affinity.translate(affinity.rotate(local, -angle, origin=(0, 0)), *origin)
    return None


def read_copper(root, nets=None):
    names = {name: net for net, name in (nets or {}).items()}
    def net_id(node):
        token = child(node, "net")[1]
        return names[token] if nets is not None and not children(root, "net") else int(token)
    primitives, vias = [], []
    for key in ("segment", "arc"):
        for n in children(root, key):
            primitives.append(Primitive(net_id(n), LAYERS.index(child(n, "layer")[1]),
                                        float(child(n, "width")[1]), xy(n, "start"), xy(n, "end"),
                                        xy(n, "mid") if key == "arc" else None))
    for n in children(root, "via"):
        if n[1] in ("blind", "micro"):
            raise UnsupportedBoard("Only full-stack through vias are supported")
        vias.append(Via(net_id(n), xy(n), float(child(n, "size")[1]), float(child(n, "drill")[1])))
    return primitives, vias


def validate_custom_rules(path):
    """Only immutable-pad manufacturing constraints are supported natively.

    Arbitrary electrical conditions are rejected, never silently discarded.
    These constraints remain in the output and are checked by native DRC.
    """
    text = re.sub(r"(?m)#.*$", "", path.read_text())
    # .kicad_dru consists of several top-level expressions.
    root = parse("(rules " + text + ")")
    if any(not isinstance(n, list) or n[0] not in ("rule", "version") for n in root[1:]):
        raise UnsupportedBoard("Unsupported custom design rule declaration")
    supported = {
        ("A.Type == 'Pad' && A.isPlated()", "annular_width"),
        ("A.Type == 'Pad' && !A.isPlated()", "hole_size"),
    }
    for rule in children(root, "rule"):
        if any(not isinstance(n, list) or n[0] not in ("condition", "constraint") for n in rule[2:]):
            raise UnsupportedBoard("Unsupported custom design rule qualifier")
        conditions = children(rule, "condition")
        constraints = children(rule, "constraint")
        if len(conditions) != 1 or not constraints or any(
                (conditions[0][1], c[1]) not in supported for c in constraints):
            raise UnsupportedBoard("Unsupported custom design rule: " + rule[1])
        for constraint in constraints:
            if len(constraint) != 3 or constraint[2][0] != "min" or not re.fullmatch(r"[0-9.]+mm", constraint[2][1]):
                raise UnsupportedBoard("Unsupported custom constraint expression")
