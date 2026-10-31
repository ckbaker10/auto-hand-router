import hashlib
import math
from pathlib import Path

import pytest
from shapely.geometry import Point, box, GeometryCollection

from auto_hand_router.cli import DEFAULT_BOARD, main
from auto_hand_router.model import Board, Pad, Route, Rules, UnsupportedBoard, Via, arc_geometry, matches_netclass, rotate_local
from auto_hand_router.output import board_text
from auto_hand_router.router import GridSearch, Options, RoutingFailure, Scene, fillet, relax
from auto_hand_router.sexpr import parse
from auto_hand_router.validate import check


def small_board(block_back=False):
    pads = [Pad("J1", "1", 1, (2, 6), (0,), Point(2, 6).buffer(0.7)),
            Pad("J2", "1", 1, (18, 6), (0,), Point(18, 6).buffer(0.7)),
            Pad("X", "1", 2, (10, 6), (0, 1) if block_back else (0,), box(9, -1, 11, 13))]
    return Board(Path("synthetic.kicad_pcb"), Path("synthetic.kicad_pro"), "", [], pads,
                 box(0, 0, 20, 12), {1: "signal", 2: "obstacle"},
                 {1: Rules(width=0.3, clearance=0.2, via_diameter=0.7, via_drill=0.3),
                  2: Rules(width=0.3, clearance=0.2)}, [], [])


def test_pad_rotation_matches_kicad_screen_coordinates():
    assert rotate_local((-2.475, -1.905), (124.925667, 73.152), 90) == pytest.approx((123.020667, 75.627))


def test_rounded_board_and_netclass_rules():
    b = Board.load(DEFAULT_BOARD)
    assert len(b.pads) == 35
    assert b.boundary.area > 900
    assert b.rules[5].width == 0.508
    assert b.rules[5].clearance == 0.254
    assert b.rules[2].width == 0.508  # KiCad's anchored regex /*GND also matches root GND
    assert b.rules[2].clearance == 0.254
    assert b.boundary.contains(Point(100, 65))
    assert not b.boundary.contains(Point(96.8, 60.46))


def test_board_unrouting_preserves_footprints_and_source():
    before = hashlib.sha256(DEFAULT_BOARD.read_bytes()).hexdigest()
    b = Board.load(DEFAULT_BOARD)
    unrouted = parse(board_text(b))
    assert len([n for n in unrouted if isinstance(n, list) and n[0] == "footprint"]) == 17
    assert not any(isinstance(n, list) and n[0] in ("segment", "via", "arc") for n in unrouted)
    assert check(b, unrouted)["unconnected_components"] == 22
    assert hashlib.sha256(DEFAULT_BOARD.read_bytes()).hexdigest() == before


def test_two_layer_obstacle_requires_legal_vias():
    b = small_board()
    scene = Scene(b)
    route, _ = GridSearch(scene, 1, Options(grid=0.4, spacing=0)).find(b.pads[0], b.pads[1])
    assert len(route.vias) == 2
    assert {layer for layer, _ in route.paths} == {0, 1}
    scene.routes.append(route)
    assert scene.route_legal(route)
    for v in route.vias:
        assert abs(v.center[0] - 10) > 1.6


def test_unroutable_wall_is_reported():
    b = small_board(block_back=True)
    with pytest.raises(RoutingFailure, match="No path"):
        GridSearch(Scene(b), 1, Options(grid=0.4, spacing=0)).find(b.pads[0], b.pads[1])


def test_arc_fillet_is_tangent_and_preserves_endpoints():
    primitives = fillet([(1, 1), (5, 1), (5, 5)], 1, 0, 0.3, GeometryCollection(), box(0, 0, 10, 10), 1)
    assert primitives[0].start == (1, 1)
    assert primitives[-1].end == (5, 5)
    arc = next(p for p in primitives if p.mid)
    center, radius, _, sweep = arc_geometry(arc.start, arc.mid, arc.end)
    assert radius == pytest.approx(1)
    assert sweep == pytest.approx(math.pi / 2)
    assert tuple(center) == pytest.approx((4, 2))
    assert arc.start == pytest.approx((4, 1))
    assert arc.end == pytest.approx((5, 2))


def test_spreading_uses_available_space_without_moving_pad_anchors():
    points = [(1, 1), (4, 1), (7, 1), (10, 1)]
    wall = box(-1, -1, 12, 0.7)
    allowed = box(0, 0, 11, 8)
    relaxed = relax(points, wall, allowed, Options(spacing=2, relax_steps=45))
    assert relaxed[0] == points[0] and relaxed[-1] == points[-1]
    assert max(y for _, y in relaxed) > 1.25
    assert Scene.legal(__import__("shapely").geometry.LineString(relaxed), wall, allowed)


def test_custom_rules_fail_explicitly(tmp_path):
    b = Board.load(DEFAULT_BOARD)
    board = tmp_path / "test.kicad_pcb"
    project = tmp_path / "test.kicad_pro"
    board.write_text(b.text)
    project.write_bytes(b.project.read_bytes())
    board.with_suffix(".kicad_dru").write_text(
        '(version 1) (rule "unimplemented" (condition "A.NetName == \'GND\'") '
        '(constraint clearance (min 2mm)))')
    with pytest.raises(UnsupportedBoard, match="Unsupported custom design rule"):
        Board.load(board)


def test_same_net_via_in_smd_pad_is_explicitly_forbidden():
    board = Board.load(DEFAULT_BOARD)
    pad = next(p for p in board.pads if p.ref == "R3" and p.number == "1")
    rule = board.rules[pad.net]
    via = Via(pad.net, pad.center, rule.via_diameter, rule.via_drill)
    result = check(board, parse(board_text(board, [], [via])))
    assert any(e["type"] == "via_in_pad" for e in result["geometry_errors"])
    search = GridSearch(Scene(board), pad.net, Options())
    x, y = search.index(pad.center)
    assert not search.via_free[y, x]


def test_via_inside_component_body_is_forbidden_even_without_pad_overlap():
    board = Board.load(DEFAULT_BOARD)
    center = (119.292333, 73.152)  # R3 body, between its two SMD pads
    rule = board.rules[7]
    via = Via(7, center, rule.via_diameter, rule.via_drill)
    result = check(board, parse(board_text(board, [], [via])))
    assert any(e["type"] == "via_in_footprint" and e["footprint"] == "R3"
               for e in result["geometry_errors"])
    search = GridSearch(Scene(board), 7, Options())
    x, y = search.index(center)
    assert not search.via_free[y, x]


@pytest.mark.parametrize("name,pattern,expected", [
    ("GND", "/*GND", True), ("/+5V", "/*V", True),
    ("/RST", "/*GND", False), ("/Trigger", "/*V", False),
])
def test_kicad_combined_netclass_matcher(name, pattern, expected):
    assert matches_netclass(name, pattern) == expected


def test_three_traces_spread_into_available_channel():
    from shapely.geometry import LineString

    board = small_board()
    board.pads = []
    board.nets = {1: "a", 2: "b", 3: "c"}
    board.rules = {n: Rules(width=0.2, clearance=0.15) for n in board.nets}
    routes = [Route(n, [(0, [(1, y), (5, y), (10, y), (15, y), (19, y)])])
              for n, y in enumerate((2.4, 3.2, 4.0), start=1)]
    scene = Scene(board, routes)
    for _ in range(3):
        for route in routes:
            points = relax(route.paths[0][1], scene.obstacles(route.net, 0),
                           scene.allowed(route.net), Options(spacing=1.5))
            route.paths = [(0, points)]
    midpoints = [LineString(r.paths[0][1]).interpolate(0.5, normalized=True).y for r in routes]
    assert midpoints[-1] - midpoints[0] > 2.0  # initial span was only 1.6 mm
    assert all(scene.route_legal(r) for r in routes)


def test_failed_search_preserves_previous_generated_artifacts(tmp_path, monkeypatch):
    import auto_hand_router.cli as cli

    destination = tmp_path / "output"
    destination.mkdir()
    (destination / ".auto-hand-router").write_text("owned")
    (destination / "routed.kicad_pcb").write_text("previous accepted result")

    def fail(*args, **kwargs):
        raise RoutingFailure("deliberately blocked fixture")

    monkeypatch.setattr(cli, "route_board", fail)
    assert main(["--output-dir", str(destination)]) == 2
    assert (destination / "routed.kicad_pcb").read_text() == "previous accepted result"
    assert not list(tmp_path.glob(".auto-hand-router-stage-*"))
