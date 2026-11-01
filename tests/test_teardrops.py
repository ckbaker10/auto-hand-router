"""Copper regressions for the pad/via transitions in the Melting KiCad images."""
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from shapely.geometry import Point, box
from shapely.ops import unary_union

from auto_hand_router.model import Board, Pad, Primitive, RuleArea, Rules, Via
from auto_hand_router.output import board_text, svg
from auto_hand_router.sexpr import parse
from auto_hand_router.teardrops import TeardropOptions, anchors, generate, read_teardrops, shoulder
from auto_hand_router.validate import check


def fixture():
    text = '(kicad_pcb (net 0 "") (net 1 "A") (net 2 "B"))'
    return Board(Path("in"), Path("pro"), text, parse(text),
                 [Pad("J1", "1", 1, (4, 5), (0,), Point(4, 5).buffer(1, quad_segs=64))],
                 box(0, 0, 18, 12), {0:"", 1:"A", 2:"B"},
                 {n:Rules() for n in range(3)}, [], [])


def test_centered_shoulders_are_symmetric_and_roundtrip_as_real_net_copper(tmp_path):
    b = fixture()
    copper = [Primitive(1, 0, .381, (4, 5), (12, 5))]
    drops, stats = generate(b, copper, [])
    assert stats["generated"] == 1
    shape = drops[0].shape
    from shapely.affinity import scale
    assert shape.hausdorff_distance(scale(shape, yfact=-1, origin=(4,5))) < 1e-9
    text = board_text(b, copper, [], drops)
    reread = read_teardrops(parse(text), b.nets)
    assert len(reread) == 1
    assert reread[0].net == 1 and reread[0].layer == 0
    assert reread[0].shape.hausdorff_distance(shape) < 1e-6
    assert check(b, parse(text))["passed"]
    svg(b, [("Teardrops", copper, [], reread)], tmp_path/"preview.svg")
    assert 'fill="#ba5735"' in (tmp_path/"preview.svg").read_text()


def test_offset_entry_has_asymmetric_shoulders_without_a_fold():
    b = fixture()
    copper = [Primitive(1, 0, .381, (4, 5.5), (12, 5.5))]
    drops, _ = generate(b, copper, [])
    assert len(drops) == 1
    shape = drops[0].shape
    assert shape.is_valid
    assert shape.intersection(b.pads[0].shape).area > .5
    assert shape.covers(Point(6, 5.5))
    assert shape.bounds[1] < 4.3 and shape.bounds[3] > 5.7


def test_short_pieces_are_followed_to_the_same_tail_as_an_unsplit_lead():
    b = fixture()
    points = [(4,5),(5.1,5),(5.2,5),(5.5,5),(6,5),(7,5),(12,5)]
    split = [Primitive(1,0,.381,a,z) for a,z in zip(points,points[1:])]
    whole = [Primitive(1,0,.381,points[0],points[-1])]
    a, _ = generate(b, split, [])
    c, _ = generate(b, whole, [])
    assert len(a) == len(c) == 1
    assert a[0].shape.hausdorff_distance(c[0].shape) < 1e-6


def test_native_arc_followed_by_a_line_uses_the_arc_length_and_tail_tangent():
    b = fixture()
    from auto_hand_router.curves import biarc
    arc = biarc((4,5),(7,8),(1,0),(0,1),1,0,.381)
    copper = arc + [Primitive(1,0,.381,(7,8),(7,11))]
    drops, _ = generate(b, copper, [])
    assert len(drops) == 1
    assert drops[0].shape.is_valid
    assert drops[0].shape.bounds[3] > 5.5
    assert check(b, parse(board_text(b,copper,[],drops)))["passed"]


def test_branch_limits_length_and_track_order_does_not_choose_an_arm():
    b = fixture()
    copper = [Primitive(1,0,.381,(4,5),(5.7,5)),
              Primitive(1,0,.381,(5.7,5),(10,7)),
              Primitive(1,0,.381,(5.7,5),(10,3))]
    drops, _ = generate(b,copper,[])
    backwards, _ = generate(b,list(reversed(copper)),[])
    assert len(drops) == len(backwards) == 1
    assert drops[0].shape.bounds[2] < 5.7
    assert drops[0].shape.equals_exact(backwards[0].shape,1e-8)


def test_inline_via_gets_two_shoulders_on_each_connected_layer():
    b = fixture()
    b.pads = []
    via = Via(1,(4,5),2.,.4)
    copper = [Primitive(1,layer,.381,a,z) for layer in (0,1)
              for a,z in [((1,5),(4,5)),((4,5),(10,5))]]
    drops,stats = generate(b,copper,[via])
    assert stats["shoulders"] == 4
    assert len(drops) == 4
    assert sum(d.layer==0 for d in drops) == 2


def test_foreign_copper_and_keepouts_shorten_or_suppress_the_shoulders():
    b = fixture()
    copper = [Primitive(1,0,.381,(4,5),(12,5)),
              Primitive(2,0,.381,(6,6),(10,6))]
    drops, _ = generate(b,copper,[])
    foreign = copper[1].line().buffer(copper[1].width/2)
    assert all(d.shape.distance(foreign) >= b.rules[1].clearance for d in drops)
    b.rule_areas = [RuleArea(box(5.1,5.3,8,6.5),(0,),True,False)]
    drops,_ = generate(b,copper,[])
    assert all(d.shape.disjoint(b.rule_areas[0].shape) for d in drops)


def test_independent_validator_checks_added_copper_and_not_only_centerlines():
    b = fixture()
    copper = [Primitive(1,0,.381,(4,5),(12,5))]
    drops,_ = generate(b,copper,[])
    b.rule_areas = [RuleArea(box(5.1,5.4,5.3,5.7),(0,),True,False)]
    assert check(b,parse(board_text(b,copper,[])))["passed"]
    result = check(b,parse(board_text(b,copper,[],drops)))
    assert not result["passed"]
    assert any(e["item"].startswith("teardrop-") for e in result["geometry_errors"])


def test_rectangular_pad_uses_a_side_tangent_and_does_not_move_tracks():
    b = fixture()
    b.pads = [replace(b.pads[0],shape=box(3,4,5,6))]
    copper = [Primitive(1,0,.381,(4,5),(12,5))]
    drops,_ = generate(b,copper,[])
    assert len(drops) == 1
    assert all(t @ np.array([1,0]) > .99 for _,t in anchors(b.pads[0].shape,(4,5),(1,0)))
    assert drops[0].shape.intersection(b.pads[0].shape).area > .5
    assert copper[0].start == (4,5) and copper[0].end == (12,5)


def test_regeneration_replaces_only_our_zones_and_accepts_named_net_syntax():
    b = fixture()
    b.named_nets = True
    b.text = '(kicad_pcb (zone (net "A") (layer "F.Cu") (name "user-zone")))'
    b.expression = parse(b.text)
    copper = [Primitive(1,0,.381,(4,5),(12,5))]
    drops,_ = generate(b,copper,[])
    text = board_text(b,copper,[],drops)
    assert len(read_teardrops(parse(text),b.nets)) == 1
    b.text, b.expression = text, parse(text)
    text2 = board_text(b,copper,[],drops)
    assert text2.count('(name "auto-hand-router:teardrop")') == 1
    assert '(name "user-zone")' in text2


@pytest.mark.parametrize("value", [0,-1,float("nan"),float("inf")])
def test_invalid_teardrop_lengths_are_rejected(value):
    with pytest.raises(ValueError):
        TeardropOptions(length_ratio=value)


def test_bezier_flanks_match_both_endpoint_tangents():
    pad = Point(0,0).buffer(1,quad_segs=64)
    shape = shoulder(pad,(0,0),(3,.5),np.array([1.,0.]),.381)
    assert shape.is_valid
    # Polygon approximation converges toward the analytic tangents at both ends.
    points = np.array(shape.exterior.coords)
    top = points[1]-points[0]
    direction = np.array([3.,.5])
    direction /= np.linalg.norm(direction)
    top /= np.linalg.norm(top)
    assert abs(top[0]*direction[1]-top[1]*direction[0]) < .08
    assert unary_union([shape,pad]).geom_type == "Polygon"


def test_reader_uses_refilled_copper_instead_of_the_untrimmed_zone_outline():
    root = parse('''(kicad_pcb (net 0 "") (net 1 "A")
        (zone (net 1) (layer "F.Cu") (name "auto-hand-router:teardrop")
          (polygon (pts (xy 1 1) (xy 8 1) (xy 8 8) (xy 1 8)))
          (filled_polygon (layer "F.Cu") (pts (xy 2 2) (xy 3 2) (xy 3 3) (xy 2 3)))))''')
    drops = read_teardrops(root,{0:"",1:"A"})
    assert drops[0].shape.area == pytest.approx(1.)


def test_pad_layers_and_foreign_drill_clearance_are_respected():
    b = fixture()
    b.pads.append(Pad("H1","",0,(6,6),(0,1),Point(6,6).buffer(.4),.8,False))
    copper = [Primitive(1,0,.381,(4,5),(12,5)),Primitive(1,1,.381,(4,5),(12,5))]
    drops,_ = generate(b,copper,[])
    assert drops and all(d.layer == 0 for d in drops)
    assert all(d.shape.distance(b.pads[-1].hole()) >= b.rules[1].hole for d in drops)


def test_teardrop_command_keeps_existing_output_before_native_setup(tmp_path):
    from auto_hand_router.teardrop_cli import main
    destination = tmp_path/"existing"
    destination.mkdir()
    (destination/"keep").write_text("previous result")
    assert main(["--board",str(tmp_path/"input.kicad_pcb"),"--output-dir",str(destination),
                 "--work-dir",str(tmp_path/"work"),"--kicad-cli","missing-kicad"]) == 1
    assert (destination/"keep").read_text() == "previous result"


def test_smaller_teardrops_reduce_length_and_contact_span_but_keep_pad_tangency():
    b = fixture()
    tracks = [Primitive(1,0,.381,(4,5),(12,5))]
    full,_ = generate(b,tracks,[],TeardropOptions(size=1.))
    small,stats = generate(b,tracks,[],TeardropOptions(size=.875))
    assert stats["size"] == .875
    # Length is measured outside the contact boundary, which stays unchanged.
    assert small[0].shape.bounds[2]-5 == pytest.approx((full[0].shape.bounds[2]-5)*.875)
    assert small[0].shape.area < full[0].shape.area
    contact = b.pads[0]
    for position,tangent in anchors(contact.shape,contact.center,(1,0),.875):
        radial = position-contact.center
        assert np.linalg.norm(radial) == pytest.approx(1.)
        assert radial @ tangent == pytest.approx(0.,abs=1e-10)
        assert abs(position[1]-5) == pytest.approx(.875)
    assert check(b,parse(board_text(b,tracks,[],small)))["passed"]


@pytest.mark.parametrize("value", [0,-1,1.01,float("nan"),float("inf")])
def test_invalid_teardrop_size_is_rejected(value):
    with pytest.raises(ValueError):
        TeardropOptions(size=value)


def test_acute_copper_tips_are_detected_on_the_combined_outline():
    from shapely.geometry import Polygon
    from auto_hand_router.teardrops import acute_tips
    base = box(0,-1,2,1)
    spike = Polygon([(1.9,-.1),(4,0),(1.9,.1)])
    assert not acute_tips(base)
    tips = acute_tips(base.union(spike))
    assert any(p.distance(Point(4,0)) < 1e-8 for p in tips)


def test_overlapping_same_net_shoulders_are_united_before_native_zone_fill():
    b = fixture()
    copper = [Primitive(1,0,.381,(4,5),(12,5)),Primitive(1,0,.381,(4,5),(12,5.5))]
    drops,stats = generate(b,copper,[])
    assert stats["shoulders"] == 2
    assert len(drops) == stats["generated"] == 1
    assert drops[0].shape.is_valid


def test_zone_holes_survive_export_readback_and_preview(tmp_path):
    from shapely.geometry import Polygon
    from auto_hand_router.teardrops import Teardrop
    b = fixture()
    polygon = Polygon(box(3,3,8,8).exterior.coords,[box(5,5,6,6).exterior.coords])
    drops = [Teardrop(1,0,polygon)]
    recovered = read_teardrops(parse(board_text(b,[],[],drops)),b.nets)
    assert recovered[0].shape.equals(polygon)
    svg(b,[("holes",[],[],recovered)],tmp_path/"holes.svg")
    assert 'fill-rule="evenodd"' in (tmp_path/"holes.svg").read_text()


def test_small_diagonal_rectangle_shoulders_do_not_overshoot_the_pad_corners():
    tangent = np.array([1.,-1.])/np.sqrt(2)
    polygon = shoulder(box(-1,-1,1,1),(0,0),(2,-2),tangent,.381,.875)
    assert polygon is not None and polygon.is_valid
    assert polygon.bounds[0] >= -1 and polygon.bounds[3] <= 1


@pytest.mark.parametrize("shape",[box(-1,-1,1,1),box(-2,-1,2,1)])
def test_rectangle_corner_radii_do_not_misclassify_the_contact_as_a_circle(shape):
    from auto_hand_router.teardrops import round_contact
    assert not round_contact(shape,(0,0))
    for position,_ in anchors(shape,(0,0),np.array([1.,-1.])/np.sqrt(2),.875):
        assert shape.boundary.distance(Point(position)) < 1e-8
