"""Regressions for visible beads, contact knees, fan-in slits and label cutouts."""
from pathlib import Path
import math
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from shapely.geometry import Point, box
from shapely.ops import unary_union

from auto_hand_router.curves import approximate, biarc, spline_target
from auto_hand_router.finishing import finish, topology_paths
from auto_hand_router.model import Board, Pad, Primitive, Rules, arc_geometry
from auto_hand_router.output import board_text, label_positions, svg
from auto_hand_router.router import Options
from auto_hand_router.sexpr import parse
from auto_hand_router.validate import check


def fork():
    points=[(4,8),(18,5),(18,11)]
    b=Board(Path("in"),Path("pro"),'(kicad_pcb (net 0 "") (net 1 "A"))',[],
            [Pad("J"+str(i),"1",1,p,(0,),Point(p).buffer(.6)) for i,p in enumerate(points)],
            box(0,0,22,16),{0:"",1:"A"},{0:Rules(),1:Rules()},[],[],
            [("J"+str(i),Point(p).buffer(.8)) for i,p in enumerate(points)])
    b.expression=parse(b.text)
    copper=[Primitive(1,0,.381,points[0],p) for p in points[1:]]
    return b,copper


def tangent(p,end=False):
    if p.mid is None:
        v=np.array(p.end)-p.start
    else:
        center,_,_,sweep=arc_geometry(p.start,p.mid,p.end)
        radial=np.array(p.end if end else p.start)-center
        v=np.array([-radial[1],radial[0]])*math.copysign(1,sweep)
    return v/np.linalg.norm(v)


def test_biarc_reproduces_a_quarter_circle_and_joins_tangentially():
    arcs=biarc((1,0),(0,1),(0,1),(-1,0),1,0,.35)
    assert len(arcs)==2
    assert arcs[0].end==arcs[1].start
    for p in arcs:
        c,r,_,_=arc_geometry(p.start,p.mid,p.end)
        assert c==pytest.approx((0,0),abs=1e-10)
        assert r==pytest.approx(1)
    assert tangent(arcs[0],True)==pytest.approx(tangent(arcs[1]),abs=1e-10)


def test_smooth_s_curve_follows_one_target_without_corner_fillet_beads():
    spline=spline_target([(0,0),(3,0),(5,1),(8,3),(10,3)],.7)
    arcs=approximate(spline,1,0,.35)
    geometry=unary_union([p.line() for p in arcs])
    assert max(Point(p).distance(geometry) for p in spline(np.linspace(0,1,1500)))<.004
    for a,b in zip(arcs,arcs[1:]):
        assert a.end==pytest.approx(b.start,abs=1e-10)
        angle=math.degrees(math.acos(float(np.clip(tangent(a,True)@tangent(b),-1,1))))
        # Almost-flat native arcs are conditioned to bounded-error chords.
        assert angle<1
    # The straight approach has zero curvature at the spline endpoint.
    a,b=spline.derivative()(0),spline.derivative(2)(0)
    assert abs(a[0]*b[1]-a[1]*b[0])<1e-8


def test_pad_anchor_is_inside_contact_not_an_artificial_boundary_knee():
    b,_=fork()
    paths=topology_paths(b,[Primitive(1,0,.381,(4,8),(18,5))],[])
    assert len(paths)==1
    assert paths[0][-1][0]==(4,8)
    assert paths[0][-1][-1]==(18,5)


def test_two_same_net_pad_entries_get_one_wider_tangent_stem():
    b,copper=fork()
    tracks,vias,stats=finish(b,copper,[],Options(spacing=0))
    assert stats["merged_fanins"]==1
    stems=[p for p in tracks if p.width>.381+1e-6]
    assert len(stems)==1
    assert stems[0].width==pytest.approx(.381*1.5)
    stem=stems[0]
    for p in [p for p in tracks if p.start==stem.end and p.width==.381]:
        assert tangent(p)==pytest.approx(tangent(stem,True),abs=1e-4)
    assert check(b,parse(board_text(b,tracks,vias)))["passed"]


def test_diagram_annotations_never_overpaint_actual_copper(tmp_path):
    b,copper=fork()
    copper.append(Primitive(1,0,.381,(2,7.5),(20,7.5)))
    geometry=unary_union([p.line().buffer(p.width/2) for p in copper]+[p.shape for p in b.pads])
    labels=label_positions(b,copper,[],0)
    assert labels
    assert all(area.disjoint(geometry) for _,_,area in labels)
    path=tmp_path/"preview.svg"
    svg(b,[("test",copper,[])],path,labels=True)
    texts=ET.parse(path).getroot().findall('.//{http://www.w3.org/2000/svg}text')
    assert all("stroke" not in t.attrib for t in texts)


def test_curves_respect_a_real_obstacle_corridor():
    from auto_hand_router.curves import smooth_path
    from auto_hand_router.router import Scene
    allowed=box(-2,-2,14,10)
    forbidden=box(4,0,7,4)
    points=[(0,0),(2,5),(8,6),(12,0)]
    curves=smooth_path(points,1,0,.35,forbidden,allowed,2.5)
    assert curves
    assert all(Scene.legal(p.line(),forbidden,allowed) for p in curves)
    assert all(p.line().is_valid for p in curves)


def test_wider_stems_are_real_obstacles_for_the_other_nets():
    from auto_hand_router.model import Route
    from auto_hand_router.router import Scene
    b,_=fork()
    b.nets[2]="other"
    b.rules[2]=Rules()
    wide=Primitive(2,0,1.5,(10,1),(10,15))
    scene=Scene(b,[Route(2,[],[],[wide])])
    assert scene.obstacles(1,0).covers(Point(11,8))


def test_no_fanin_merge_between_copper_layers():
    from dataclasses import replace
    b,copper=fork()
    b.pads=[replace(p,layers=(0,1),drill=.5) for p in b.pads]
    copper[1]=replace(copper[1],layer=1)
    tracks,vias,stats=finish(b,copper,[],Options(spacing=0))
    assert stats["merged_fanins"]==0
    assert all(p.width==.381 for p in tracks)
    assert check(b,parse(board_text(b,tracks,vias)))["passed"]


def test_nearly_flat_native_arc_is_conditioned_to_a_short_chord():
    from auto_hand_router.curves import tangent_arc
    arc=tangent_arc((0,0),(.4,.0003),(1,0),1,0,.35)
    assert arc.mid is None


def test_unused_via_is_removed_before_it_can_force_a_corner():
    from auto_hand_router.finishing import remove_single_layer_vias
    from auto_hand_router.model import Via
    b,_=fork()
    a=Primitive(1,0,.381,(4,8),(10,8))
    c=Primitive(1,0,.381,(10,8),(18,5))
    via=Via(1,(10,8),.889,.381)
    assert remove_single_layer_vias(b,[a,c],[via])==[]
    two_sided=Primitive(1,1,.381,(10,8),(18,5))
    assert remove_single_layer_vias(b,[a,two_sided],[via])==[via]
