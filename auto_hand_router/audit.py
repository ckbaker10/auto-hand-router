"""Independent project invariants and schematic-to-physical pad mapping."""
from collections import Counter
import math
import xml.etree.ElementTree as ET

from .model import arc_geometry
from .sexpr import child, children


def project_audit(source, routed, netlist):
    def pad_key(board, pad):
        return (pad.ref, pad.number, board.nets[pad.net], pad.layers, pad.plated,
                tuple(round(c, 6) for c in pad.center), round(pad.drill, 6),
                tuple(round(v, 6) for v in pad.shape.bounds),
                tuple(round(v, 6) for v in pad.hole().bounds) if pad.drill else ())
    if Counter(pad_key(source, p) for p in source.pads) != Counter(pad_key(routed, p) for p in routed.pads):
        raise ValueError("Pad position, size, hole, layer or net changed")
    def footprints(board):
        return sorted((child(f, "uuid")[1], f[1], tuple(child(f, "at")[1:]), child(f, "layer")[1])
                      for f in children(board.expression, "footprint"))
    if footprints(source) != footprints(routed):
        raise ValueError("Footprint identity or placement changed")
    if source.boundary.symmetric_difference(routed.boundary).area > 1e-7:
        raise ValueError("Board contour changed")
    if len(source.rule_areas) != len(routed.rule_areas) or any(
            a.shape.symmetric_difference(b.shape).area > 1e-7 or
            (a.layers, a.tracks, a.vias) != (b.layers, b.tracks, b.vias)
            for a, b in zip(source.rule_areas, routed.rule_areas)):
        raise ValueError("Mechanical/copper rule areas changed")
    expected = {}
    for net in ET.parse(netlist).getroot().findall("./nets/net"):
        for node in net.findall("node"):
            expected[node.attrib["ref"], node.attrib["pin"]] = net.attrib["name"]
    checked = 0
    for pad in routed.pads:
        if pad.plated and pad.net:
            actual = routed.nets[pad.net]
            if expected.get((pad.ref, pad.number)) != actual:
                raise ValueError(f"Schematic mismatch: {pad.ref}.{pad.number} {actual}")
            checked += 1
    return dict(passed=True, footprints=len(footprints(routed)), pads=len(routed.pads),
                checked_pad_nets=checked, placement_unchanged=True, outline_unchanged=True,
                holes_unchanged=True, rule_areas_unchanged=True,
                rule_areas=len(routed.rule_areas), schematic_pad_mapping=True)


def tangency_audit(board, threshold=1.0):
    """Degree-two free joints; pads, vias and branches are explicitly excluded."""
    import numpy as np
    from shapely.geometry import Point
    adjacency = {}
    for p in board.original_primitives:
        for point in (p.start, p.end):
            key = (p.net, p.layer, tuple(round(x, 6) for x in point))
            adjacency.setdefault(key, []).append(p)
    findings, free = [], 0
    def inward(primitive, point):
        if primitive.mid is None:
            other = primitive.end if math.dist(point, primitive.start) < 1e-5 else primitive.start
            vector = np.array(other) - point
        else:
            center, _, _, sweep = arc_geometry(primitive.start, primitive.mid, primitive.end)
            radial = np.array(point) - center
            vector = np.array([-radial[1], radial[0]]) * math.copysign(1, sweep)
            if math.dist(point, primitive.end) < 1e-5:
                vector = -vector
        return vector / np.linalg.norm(vector)
    for (net, layer, point), items in adjacency.items():
        if len(items) != 2:
            continue
        if any(p.net == net and layer in p.layers and p.shape.buffer(2e-6).covers(Point(point)) for p in board.pads):
            continue
        if any(v.net == net and math.dist(v.center, point) <= v.diameter/2 + 2e-6 for v in board.original_vias):
            continue
        free += 1
        a, b = (inward(p, point) for p in items)
        angle = math.degrees(math.acos(float(np.clip(-a @ b, -1, 1))))
        if angle >= threshold:
            findings.append(dict(net=board.nets[net], layer=layer, point=point, angle_deg=angle))
    return dict(free_joints=free, free_kinks=len(findings), threshold_deg=threshold, findings=findings,
                scope="G1 tangency at degree-two free joints; terminals and branches excluded")
