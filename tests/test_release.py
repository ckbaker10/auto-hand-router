"""Manufacturing geometry, topology preservation and safe candidate publication."""
from dataclasses import replace
import json
from pathlib import Path

from shapely.geometry import Point, box

from auto_hand_router.cli import DEFAULT_BOARD
from auto_hand_router.finishing import finish, topology_paths
from auto_hand_router.model import Board, Pad, Primitive, RuleArea, Rules
from auto_hand_router.output import board_text
from auto_hand_router.release import main, profile_project
from auto_hand_router.router import Options, Scene
from auto_hand_router.sexpr import parse
from auto_hand_router.validate import check


def tiny():
    return Board(Path("in"), Path("pro"), "", [],
                 [Pad("J1", "1", 1, (2, 2), (0,), Point(2, 2).buffer(.5)),
                  Pad("J2", "1", 1, (18, 2), (0,), Point(18, 2).buffer(.5)),
                  Pad("J3", "1", 1, (10, 14), (0,), Point(10, 14).buffer(.5))],
                 box(0, 0, 20, 16), {0: "", 1: "signal"}, {0: Rules(), 1: Rules()}, [], [])


def test_seed_t_junction_and_pad_contacts_stay_fixed_during_shaping():
    b = tiny()
    copper = [Primitive(1, 0, .381, (2, 2), (18, 2)),
              Primitive(1, 0, .381, (10, 2), (10, 14))]
    paths = topology_paths(b, copper, [])
    junction_paths = [points for _, _, _, points in paths if (10, 2) in (points[0], points[-1])]
    assert len(junction_paths) == 3
    tracks, _, _ = finish(b, copper, [], Options(spacing=1.2))
    assert sum((10, 2) in (p.start, p.end) for p in tracks) == 3
    # Physical union still reaches each pad and the shared branch point.
    from shapely.ops import unary_union
    union = unary_union([p.line().buffer(p.width/2) for p in tracks])
    assert all(union.intersects(p.shape) for p in b.pads)
    assert union.geom_type == "Polygon"


def test_rule_area_excludes_tracks_and_vias_independently():
    b = tiny()
    b.rule_areas = [RuleArea(box(7, 6, 13, 10), (0, 1), tracks=False, vias=True)]
    scene = Scene(b)
    assert not scene.obstacles(1, 0).intersects(Point(10, 8))
    assert scene.obstacles(1, 0, via=True).contains(Point(10, 8))
    b.rule_areas[0].tracks = True
    assert scene.obstacles(1, 0).contains(Point(10, 8))


def test_slot_ends_are_obstacles_not_only_the_slot_center():
    b = tiny()
    b.pads.append(Pad("H", "", 0, (10, 8), (0, 1), box(7, 7.5, 13, 8.5),
                      drill=1, plated=False, drill_shape=box(7, 7.5, 13, 8.5)))
    assert Scene(b).obstacles(1, 0).contains(Point(12.9, 8))
    assert Scene(b).obstacles(1, 0, via=True).contains(Point(13.1, 8))


def test_tableless_kicad10_names_roundtrip_without_changing_nets(tmp_path):
    board = Board.load(DEFAULT_BOARD)
    # Upgrade syntax alone; the physical data and desired net membership stay identical.
    text = board.text
    from auto_hand_router.sexpr import children, child
    replacements = []
    for node in children(board.expression, "net"):
        replacements.append((node.start, node.end, ""))
    for fp in children(board.expression, "footprint"):
        for pad in children(fp, "pad"):
            n = child(pad, "net")
            if n:
                replacements.append((n.start, n.end, "(net " + json.dumps(n[2]) + ")"))
    # Remove existing copper before exercising tableless parsing.
    for kind in ("segment", "arc", "via"):
        replacements.extend((n.start, n.end, "") for n in children(board.expression, kind))
    for a, z, replacement in sorted(replacements, reverse=True):
        text = text[:a] + replacement + text[z:]
    path = tmp_path / "named.kicad_pcb"
    path.write_text(text)
    path.with_suffix(".kicad_pro").write_bytes(board.project.read_bytes())
    named = Board.load(path)
    assert named.named_nets
    assert {(p.ref, p.number):named.nets[p.net] for p in named.pads} == {
        (p.ref, p.number):board.nets[p.net] for p in board.pads}
    net = next(p.net for p in named.pads if p.net)
    trace = Primitive(net, 0, named.rules[net].width, (100, 70), (101, 70))
    path.write_text(board_text(named, [trace], []))
    reread = Board.load(path)
    assert reread.nets[reread.original_primitives[0].net] == named.nets[net]


def test_profile_only_changes_candidate_and_keeps_stronger_limits(tmp_path):
    b = Board.load(DEFAULT_BOARD)
    path = tmp_path / "candidate.kicad_pcb"
    path.write_text(board_text(b))
    pro = json.loads(b.project.read_text())
    pro["board"]["design_settings"]["rules"]["min_clearance"] = .7
    path.with_suffix(".kicad_pro").write_text(json.dumps(pro))
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dict(name="test", limits_mm=dict(kicad_rules=dict(min_clearance=.25)),
        routing=dict(clearance_mm=.25,signal_width_mm=.35,power_width_mm=.6,
                     via_diameter_mm=.8,via_drill_mm=.4))))
    original = b.project.read_bytes()
    profile_project(path, profile, dict(routing=dict(power_net_patterns=["/supply"])))
    updated = json.loads(path.with_suffix(".kicad_pro").read_text())
    assert updated["board"]["design_settings"]["rules"]["min_clearance"] == .7
    assert any(c["name"] == "Power" and c["track_width"] >= .6 for c in updated["net_settings"]["classes"])
    assert b.project.read_bytes() == original


def test_failed_native_setup_keeps_previous_output_and_source(tmp_path, monkeypatch):
    from auto_hand_router import release
    def fail(_):
        raise RuntimeError("test backend unavailable")
    monkeypatch.setattr(release, "Native", fail)
    output = tmp_path / "good"
    output.mkdir()
    (output / ".auto-hand-router").write_text("owned")
    (output / "report.json").write_text("previous success")
    source = DEFAULT_BOARD.read_bytes()
    assert main(["--board", str(DEFAULT_BOARD), "--output-dir", str(output),
                 "--work-dir", str(tmp_path / "work"), "--freerouting-jar", str(tmp_path / "none")]) == 1
    assert (output / "report.json").read_text() == "previous success"
    assert DEFAULT_BOARD.read_bytes() == source
    assert not list(tmp_path.glob(".ahr-*"))


def test_slot_hole_and_via_validator_rejects_nearby_barrel():
    b = tiny()
    p = b.pads[0]
    slot = replace(p, ref="H", net=0, plated=False, center=(10, 8), drill=1,
                   shape=box(7, 7.5, 13, 8.5), drill_shape=box(7, 7.5, 13, 8.5))
    b.pads.append(slot)
    # Use a tiny numeric syntax tree for the independent checker.
    result = check(b, parse('(kicad_pcb (net 0 "") (net 1 "signal") '
                           '(via (at 13.2 8) (size .889) (drill .381) (net 1)))'))
    assert any(e["type"] == "hole_to_hole" for e in result["geometry_errors"])


def test_topology_handles_a_line_with_no_contact_boundary_intersection():
    b = tiny()
    # A free interior segment produced an empty LineString intersection in GEOS.
    paths = topology_paths(b, [Primitive(1, 0, .381, (7, 7), (9, 7))], [])
    assert len(paths) == 1
    assert paths[0][-1] == [(7, 7), (9, 7)]


def test_component_repair_connects_real_copper_and_checkpoints_each_bridge():
    from auto_hand_router.repair import repair
    b = tiny()
    b.text = '(kicad_pcb (net 0 "") (net 1 "signal"))'
    b.expression = parse(b.text)
    checkpoints = []
    tracks, vias, stats = repair(b, [Primitive(1, 0, .381, (2, 2), (10, 2))], [],
                                 Options(grid=.4, spacing=0),
                                 checkpoint=lambda p, v: checkpoints.append((len(p), len(v))))
    assert stats
    assert len(checkpoints) == len(stats)
    assert check(b, parse(board_text(b, tracks, vias)))["passed"]


def test_module_interior_policy_preserves_pad_and_mechanical_exclusions():
    from auto_hand_router.policy import via_policy
    b = tiny()
    b.pads = [replace(p,ref="U1",number=str(i),drill=1,layers=(0,1)) for i,p in enumerate(b.pads)]
    b.expression = parse('(kicad_pcb (footprint "Example:module" (property "Reference" "U1")))')
    b.via_keepouts = [("U1",box(1,1,19,15)),("H1",box(17,12,19,14))]
    b.rule_areas = [RuleArea(box(17,12,19,14),(0,1),True,True)]
    relaxed, modules = via_policy(b,"module-interiors")
    assert modules == ["U1"]
    assert len(b.via_keepouts) == 2  # Original immutable input model is retained.
    obstacles = Scene(relaxed).obstacles(1,0,via=True)
    assert not obstacles.covers(Point(10,8))
    assert obstacles.covers(Point(b.pads[0].center))
    assert obstacles.covers(Point(18,13))


def test_bounded_ripup_routes_crossing_pth_nets_on_separate_surfaces():
    from auto_hand_router.repair import ripup_repair
    b=tiny()
    b.text='(kicad_pcb (net 0 "") (net 1 "A") (net 2 "B"))'
    b.expression=parse(b.text)
    b.nets={0:"",1:"A",2:"B"}
    b.rules={n:Rules() for n in b.nets}
    b.pads=[Pad("U1","1",1,(2,8),(0,1),Point(2,8).buffer(.55),.5),
            Pad("J1","1",1,(18,8),(0,1),Point(18,8).buffer(.55),.5),
            Pad("J2","1",2,(10,.8),(0,1),Point(10,.8).buffer(.55),.5),
            Pad("J3","1",2,(10,15.2),(0,1),Point(10,15.2).buffer(.55),.5)]
    b.via_keepouts=[("U1",box(1,1,9,15))]
    wall=[Primitive(2,layer,.381,(10,.8),(10,15.2)) for layer in (0,1)]
    tracks,vias,stats=ripup_repair(b,wall,[],Options(grid=.4,spacing=0),limit=1)
    assert stats[0]["ripup_net"]=="B"
    assert stats[0]["priority_net"]=="A"
    assert check(b,parse(board_text(b,tracks,vias)))["passed"]
