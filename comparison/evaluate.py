"""Preserve reference placement, import copper verbatim, run independent and native DRC."""
from collections import Counter
import json
from pathlib import Path
import shutil
import sys

import numpy as np
from shapely.ops import unary_union

from auto_hand_router.cli import cli_environment, compare_drc, drc
from auto_hand_router.model import ARC_ERROR, Board, Primitive, Via
from auto_hand_router.output import board_text, svg
from auto_hand_router.sexpr import parse
from auto_hand_router.validate import check


def copper(data, board):
    names = {name: net for net, name in board.nets.items()}
    tracks, vias = [], []
    for t in data["tracks"]:
        if t["layer"] not in (0, 1):
            raise ValueError("Unsupported layer")
        tracks.append(Primitive(net=names[t["net"]], layer=t["layer"], width=t["width"],
                                start=tuple(t["start"]), end=tuple(t["end"]),
                                mid=tuple(t["mid"]) if t.get("mid") is not None else None))
    for v in data["vias"]:
        vias.append(Via(net=names[v["net"]], center=tuple(v["center"]),
                        diameter=v["diameter"], drill=v["drill"]))
    return tracks, vias


def uniform_spacing(board, tracks, vias):
    """Length-weighted samples: unaffected by exporter primitive segmentation."""
    from shapely.geometry import Point
    samples = []
    pad_region = unary_union([p.shape for p in board.pads if p.plated]).buffer(0.35)
    per_net = {}
    for layer in (0, 1):
        for net in board.nets:
            shapes = [p.shape for p in board.pads if p.net != net and p.net and layer in p.layers]
            shapes += [t.line().buffer(t.width/2 + ARC_ERROR) for t in tracks if t.net != net and t.layer == layer]
            shapes += [Point(v.center).buffer(v.diameter/2) for v in vias if v.net != net]
            per_net[layer, net] = unary_union(shapes)
    # Merge same-net centerline fragments before uniform sampling, to avoid each
    # exporter receiving extra endpoint samples for tiny segments.
    from shapely.ops import linemerge
    for layer in (0, 1):
        for net in board.nets:
            widths = {t.width for t in tracks if t.net == net and t.layer == layer}
            for width in widths:
                lines = [t.line() for t in tracks if t.net == net and t.layer == layer and t.width == width]
                merged = unary_union(lines)
                if merged.geom_type == "MultiLineString":
                    merged = linemerge(merged)
                for line in getattr(merged, "geoms", [merged]):
                    n = max(1, int(np.ceil(line.length / 0.25)))
                    for i in range(n):
                        pt = line.interpolate((i+0.5)/n, normalized=True)
                        if not pad_region.covers(pt):
                            samples.append((pt.distance(per_net[layer, net])-width/2, line.length/n))
    if not samples:
        return None
    return dict(mean_gap_mm=sum(g*w for g,w in samples)/sum(w for _,w in samples),
                sampled_length_mm=sum(w for _,w in samples), sample_count=len(samples),
                pitch_mm=0.25, pad_escape_exclusion_mm=0.35)


def main():
    comp, binary, destination = map(Path, sys.argv[1:4])
    destination.mkdir(parents=True, exist_ok=True)
    evidence = comp / "runs/evaluation"
    evidence.mkdir(exist_ok=True)
    board = Board.load(comp / "inputs/555.kicad_pcb")
    baseline = drc(binary, board.source, evidence / "baseline.json", cli_environment(binary, evidence))
    results, panels = {}, []
    for name, path in zip(sys.argv[4::2], sys.argv[5::2]):
        data = json.loads(Path(path).read_text())
        tracks, vias = copper(data, board)
        text = board_text(board, tracks, vias)
        output = destination / (name + ".kicad_pcb")
        output.write_text(text)
        shutil.copyfile(board.project, output.with_suffix(".kicad_pro"))
        native = drc(binary, output, evidence / (name + ".json"), cli_environment(binary, evidence))
        own = check(board, parse(text))
        own["geometry_error_counts"] = dict(Counter(e["type"] for e in own["geometry_errors"]))
        results[name] = dict(independent=own, kicad=compare_drc(baseline, native),
                             uniform_spacing=uniform_spacing(board, tracks, vias),
                             engine_elapsed_seconds=data.get("elapsed_seconds"))
        panels.append((name, tracks, vias))
        print(name, json.dumps({"kicad":results[name]["kicad"], "geometry":own["geometry_error_counts"],
                               "unconnected":own["unconnected_components"], "vias":len(vias),
                               "length_mm":own["track_length_mm"]}))
    (destination / "results.json").write_text(json.dumps(results, indent=2))
    import cairosvg
    main_panels = [p for p in panels if p[0] in ("own", "kreisel", "freerouting", "tscircuit")]
    rounded_panels = [p for p in panels if p[0] in ("own", "freerouting") or "rounded" in p[0]]
    for label, group in [("comparison", main_panels), ("rounding", rounded_panels)]:
        if not group:
            continue
        path = destination / (label + ".svg")
        svg(board, group, path)
        path.write_text(path.read_text().replace(
            "Auto-Hand-Router · 555 reference PoC · same placement and design rules",
            "555 Routingvergleich · gleiche Platzierung und Netze · Adapterdetails im Bericht"))
        cairosvg.svg2png(url=str(path),
                        write_to=str(destination / (label + ".png")))


if __name__ == "__main__":
    main()
