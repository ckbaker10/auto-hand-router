"""Preserve board source, append native copper, and draw standalone SVG comparisons."""

from html import escape
import json
import math
from pathlib import Path
import uuid

from .model import LAYERS, arc_geometry


def copper_text(primitives, vias, net_names=None):
    forms = []
    def fmt(point):
        return " ".join(f"{v:.6f}" for v in point)
    def net_token(net):
        return json.dumps(net_names[net]) if net_names is not None else str(net)
    for i, p in enumerate(primitives):
        uid = uuid.uuid5(uuid.NAMESPACE_URL, f"auto-hand-router:poc:{i}:{p}")
        mid = f"(mid {fmt(p.mid)}) " if p.mid is not None else ""
        forms.append(f'  ({"arc" if p.mid else "segment"} (start {fmt(p.start)}) {mid}'
                     f'(end {fmt(p.end)}) (width {p.width:.6f}) '
                     f'(layer "{LAYERS[p.layer]}") (net {net_token(p.net)}) (uuid "{uid}"))')
    for i, v in enumerate(vias):
        uid = uuid.uuid5(uuid.NAMESPACE_URL, f"auto-hand-router:poc:via:{i}:{v}")
        forms.append(f'  (via (at {fmt(v.center)}) (size {v.diameter:.6f}) (drill {v.drill:.6f}) '
                     f'(layers "F.Cu" "B.Cu") (net {net_token(v.net)}) (uuid "{uid}"))')
    return "\n".join(forms)


def teardrop_text(board, teardrops):
    from .teardrops import ZONE_NAME
    forms = []
    for i, drop in enumerate(teardrops):
        uid = uuid.uuid5(uuid.NAMESPACE_URL, f"auto-hand-router:teardrop:{i}:{drop.net}:{drop.layer}:{drop.shape.wkt}")
        net = json.dumps(board.nets[drop.net]) if board.named_nets else str(drop.net)
        name = "" if board.named_nets else f' (net_name {json.dumps(board.nets[drop.net])})'
        polygons = " ".join('(polygon (pts '+" ".join(f"(xy {x:.6f} {y:.6f})" for x,y in ring.coords[:-1])+'))'
                            for ring in [drop.shape.exterior,*drop.shape.interiors])
        forms.append(f'  (zone (net {net}){name} (layer "{LAYERS[drop.layer]}") '
                     f'(uuid "{uid}") (name "{ZONE_NAME}") (hatch edge 0.5) '
                     f'(priority {1000+i}) (connect_pads yes (clearance {board.rules[drop.net].clearance:.6f})) '
                     f'(min_thickness 0.01) (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2)) '
                     f'{polygons})')
    return "\n".join(forms)


def board_text(board, primitives=(), vias=(), teardrops=()):
    removals = [(n.start, n.end) for n in board.expression if isinstance(n, list)
                and n and n[0] in ("segment", "arc", "via")]
    # Filled copper must be regenerated after any route change; zone outlines,
    # rules and UUIDs are retained exactly.
    from .sexpr import child, children
    from .teardrops import ZONE_NAME
    for zone in children(board.expression, "zone"):
        if child(zone, "name") == ["name", ZONE_NAME]:
            removals.append((zone.start, zone.end))
        else:
            removals.extend((n.start, n.end) for n in children(zone, "filled_polygon"))
    text = board.text
    for start, end in sorted(removals, reverse=True):
        text = text[:start] + text[end:]
    end = text.rfind(")")
    return (text[:end].rstrip() + "\n" + copper_text(primitives, vias, board.nets if board.named_nets else None)
            + "\n" + teardrop_text(board, teardrops) + "\n)\n")


def primitive_svg(p):
    x, y = p.start
    xx, yy = p.end
    d = f"M{x},{y} "
    if p.mid is None:
        return d + f"L{xx},{yy}"
    _, radius, _, sweep = arc_geometry(p.start, p.mid, p.end)
    return d + f"A{radius},{radius} 0 {int(abs(sweep) > math.pi)} {int(sweep > 0)} {xx},{yy}"


def polygon_svg(shape):
    return " ".join(f"{x},{y}" for x, y in shape.exterior.coords)


def area_svg(shape):
    return " ".join("M"+" L".join(f"{x},{y}" for x,y in ring.coords)+" Z"
                    for ring in [shape.exterior,*shape.interiors])


def label_positions(board, primitives, vias, layer, teardrops=()):
    """Place diagram annotations in free space; never erase copper with halos."""
    from shapely.geometry import Point, box
    from shapely.strtree import STRtree
    shapes = [p.line().buffer(p.width/2) for p in primitives if p.layer==layer]
    shapes += [p.shape for p in board.pads if layer in p.layers]
    shapes += [Point(v.center).buffer(v.diameter/2) for v in vias]
    shapes += [t.shape for t in teardrops if t.layer == layer]
    tree = STRtree(shapes)
    placed, boxes = [], []
    for ref,region in board.display_regions or board.via_keepouts:
        x,y = region.representative_point().coords[0]
        width,height = max(.8,len(ref)*.62),1.1
        offsets = sorted(((dx*.7,dy*.7) for dx in range(-4,5) for dy in range(-4,5)),
                         key=lambda p:p[0]*p[0]+p[1]*p[1])
        for dx,dy in offsets:
            xx,yy = x+dx,y+dy
            area = box(xx-width/2-.15,yy-height-.15,xx+width/2+.15,yy+.2)
            if not board.boundary.covers(area) or len(tree.query(area,predicate="intersects")):
                continue
            if any(area.intersects(other) for other in boxes):
                continue
            boxes.append(area)
            placed.append((ref,(xx,yy),area))
            break
    return placed


def svg(board, panels, destination, labels=False):
    """Panels: (title, primitives, vias[, teardrops]), each layer separately."""
    minx, miny, maxx, maxy = board.boundary.bounds
    w, h = maxx - minx, maxy - miny
    scale = 14
    panel_w, panel_h = (w + 7) * scale, (h + 8) * scale
    total_w, total_h = panel_w * len(panels), panel_h * 2 + 64
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w:.0f}" height="{total_h:.0f}" '
             f'viewBox="0 0 {total_w} {total_h}">',
             '<rect width="100%" height="100%" fill="#f0f3f6"/>',
             '<style>text{font-family:Arial,sans-serif;fill:#172735}.label{font-size:18px;font-weight:bold}</style>',
             '<text x="26" y="28" class="label">Auto-Hand-Router · 555 reference PoC · same placement and design rules</text>']
    for col, panel in enumerate(panels):
        title, primitives, vias = panel[:3]
        teardrops = panel[3] if len(panel) > 3 else ()
        for layer in (0, 1):
            x0, y0 = col * panel_w + 25, layer * panel_h + 65
            color = "#ba5735" if layer == 0 else "#347d89"
            parts.append(f'<text x="{x0}" y="{y0 - 12}" class="label">{escape(title)} · {LAYERS[layer]}</text>')
            parts.append(f'<g transform="translate({x0 - minx * scale},{y0 - miny * scale}) scale({scale})">')
            parts.append(f'<polygon points="{polygon_svg(board.boundary)}" fill="#fbf8eb" stroke="#8c9a97" stroke-width="0.12"/>')
            if labels:
                for area in board.rule_areas:
                    if layer in area.layers and area.tracks:
                        parts.append(f'<polygon points="{polygon_svg(area.shape)}" fill="#9d6b65" fill-opacity="0.12" '
                                     'stroke="#b68f88" stroke-width="0.08"/>')
            for _, region in board.via_keepouts:
                for shape in getattr(region, "geoms", [region]):
                    if shape.geom_type == "Polygon":
                        parts.append(f'<polygon points="{polygon_svg(shape)}" fill="none" stroke="#aeb9b3" '
                                     'stroke-width="0.045" stroke-dasharray="0.18,0.14"/>')
            for drop in teardrops:
                if drop.layer == layer:
                    for shape in getattr(drop.shape, "geoms", [drop.shape]):
                        parts.append(f'<path d="{area_svg(shape)}" fill="{color}" fill-rule="evenodd"/>')
            for p in primitives:
                if p.layer == layer:
                    parts.append(f'<path d="{primitive_svg(p)}" fill="none" stroke="{color}" stroke-width="{p.width}" '
                                 'stroke-linecap="round" stroke-linejoin="round"/>')
            for pad in board.pads:
                if layer in pad.layers and pad.plated:
                    parts.append(f'<polygon points="{polygon_svg(pad.shape)}" fill="{color}"/>')
                if pad.drill_shape is not None:
                    parts.append(f'<polygon points="{polygon_svg(pad.drill_shape)}" fill="#eef2f5" stroke="#85908a" stroke-width="0.05"/>')
                elif pad.drill:
                    parts.append(f'<circle cx="{pad.center[0]}" cy="{pad.center[1]}" r="{pad.drill/2}" fill="#eef2f5" stroke="#85908a" stroke-width="0.05"/>')
            for v in vias:
                parts.append(f'<circle cx="{v.center[0]}" cy="{v.center[1]}" r="{v.diameter/2}" fill="{color}"/>')
                parts.append(f'<circle cx="{v.center[0]}" cy="{v.center[1]}" r="{v.drill/2}" fill="#eef2f5"/>')
            if labels:
                for reference,(x,y),_ in label_positions(board,primitives,vias,layer,teardrops):
                    parts.append(f'<text x="{x}" y="{y}" text-anchor="middle" font-size="0.9" '
                                 f'fill="#334940">{escape(reference)}</text>')
            parts.append('</g>')
    parts.append('</svg>')
    Path(destination).write_text("\n".join(parts))
