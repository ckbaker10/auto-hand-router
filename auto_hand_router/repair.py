"""Finish physically disconnected components with the reference maze router."""
import math

from .model import Route
from .output import board_text
from .router import GridSearch, Scene, simplify
from .sexpr import parse
from .validate import check


def repair(board, primitives, vias, options, progress=None, checkpoint=None, first_net=None):
    routes = [Route(net, [], [v for v in vias if v.net == net], [p for p in primitives if p.net == net])
              for net in sorted({p.net for p in primitives} | {v.net for v in vias})]
    scene = Scene(board, routes)
    attempted = set()
    stats = []
    while True:
        result = check(board, parse(board_text(board, primitives, vias)))
        if not result["unconnected_components"]:
            return primitives, vias, stats
        connections = []
        for net in result["connectivity"]:
            groups = net["pad_groups"]
            for a, aa in enumerate(groups):
                for bb in groups[:a]:
                    for i in aa:
                        for j in bb:
                            key = tuple(sorted((i, j)))
                            if key not in attempted:
                                preferred = 0 if board.nets[board.pads[i].net] == first_net else 1
                                connections.append((preferred, math.dist(board.pads[i].center, board.pads[j].center), key))
        if not connections:
            raise RuntimeError("No further component bridges can be attempted")
        _, _, pair = min(connections)
        attempted.add(pair)
        a, b = [board.pads[i] for i in pair]
        if progress:
            progress(f"Repair {board.nets[a.net]} {a.ref}.{a.number} -> {b.ref}.{b.number}")
        search = GridSearch(scene, a.net, options)
        try:
            route, expanded = search.find(a, b)
        except RuntimeError as exc:
            if progress:
                progress("Blocked bridge: " + str(exc))
            if first_net == board.nets[a.net] and sum(p.plated and p.net == a.net for p in board.pads) == 2:
                raise
            if len(attempted) > 32:
                raise
            continue
        route.paths = [(layer, simplify(points, search.forbidden[layer], search.allowed))
                       for layer, points in route.paths]
        from .model import Primitive
        route.primitives = [Primitive(a.net, layer, board.rules[a.net].width, x, y)
                            for layer, points in route.paths for x, y in zip(points, points[1:])]
        scene.routes.append(route)
        primitives.extend(route.primitives)
        vias.extend(route.vias)
        stats.append(dict(net=board.nets[a.net], expanded=expanded, vias=len(route.vias)))
        if checkpoint:
            checkpoint(primitives, vias)


def ripup_repair(board, primitives, vias, options, progress=None, checkpoint=None, limit=4):
    """Replan a nearby whole net when a disconnected terminal is boxed in.

    Each trial starts from the same snapshot; failed trials are discarded.
    The disconnected net is routed before reconnecting the removed net.
    """
    from shapely.geometry import Point
    result = check(board, parse(board_text(board, primitives, vias)))
    disconnected = [n for n in result["connectivity"] if n["components"] > 1]
    if not disconnected:
        return primitives, vias, []
    critical = disconnected[0]
    pads = [board.pads[i] for group in critical["pad_groups"] for i in group]
    body_size = {ref:region.area for ref,region in board.via_keepouts}
    # The terminal in the largest body has the least freedom to change layers.
    # Prioritise barriers around that terminal, rather than the easy connector.
    trapped = max(pads, key=lambda p:body_size.get(p.ref, 0))
    points = [Point(trapped.center)]
    distances = {}
    for p in primitives:
        if board.nets[p.net] == critical["net"]:
            continue
        distances[p.net] = min(distances.get(p.net, math.inf), min(p.line().distance(pt) for pt in points))
    failures = []
    for net in sorted(distances, key=lambda n:(distances[n],sum(p.net==n for p in primitives)))[:limit]:
        if progress:
            progress(f"Rip up {board.nets[net]} to give {critical['net']} priority")
        trial = [p for p in primitives if p.net != net]
        barrels = [v for v in vias if v.net != net]
        # Two PTH terminals can deliberately use one surface, leaving the other
        # surface available to the neighbouring power net. Try both surfaces
        # before a general multilayer repair that may box that neighbour in.
        if len(pads) == 2 and all(p.layers == (0,1) for p in pads):
            from dataclasses import replace
            from .model import Primitive
            for layer in (0,1):
                if progress:
                    progress(f"Try priority net on layer {layer} without vias")
                routes = [Route(n, [], [v for v in barrels if v.net==n], [p for p in trial if p.net==n])
                          for n in {p.net for p in trial}]
                search = GridSearch(Scene(board,routes), pads[0].net, options)
                search.via_free[:] = False
                try:
                    path, _ = search.find(replace(pads[0],layers=(layer,)),replace(pads[1],layers=(layer,)))
                    path.paths = [(surface,simplify(x,search.forbidden[surface],search.allowed)) for surface,x in path.paths]
                    new = [Primitive(pads[0].net,surface,board.rules[pads[0].net].width,a,b)
                           for surface,points in path.paths for a,b in zip(points,points[1:])]
                    repaired, vv, stats = repair(board, trial+new, list(barrels), options, progress=progress)
                    if checkpoint:
                        checkpoint(repaired,vv)
                    return repaired,vv,[dict(ripup_net=board.nets[net],priority_net=critical["net"],
                                               priority_layer=layer,bridges=stats)]
                except RuntimeError as exc:
                    failures.append(str(exc))
        try:
            trial, barrels, stats = repair(board, trial, barrels, options, progress=progress,
                                           first_net=critical["net"])
        except RuntimeError as exc:
            failures.append(str(exc))
            continue
        if checkpoint:
            checkpoint(trial, barrels)
        return trial, barrels, [dict(ripup_net=board.nets[net], priority_net=critical["net"], bridges=stats)]
    raise RuntimeError("Bounded rip-up repair failed: " + "; ".join(failures))
