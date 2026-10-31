"""C2 spline targets approximated by tangent-continuous native biarcs.

Native circular arcs cannot have exact G2 continuity when their radii differ.
The approximation follows one smooth target rather than alternating unrelated
corner fillets and short straight pieces. Every exported arc is clearance tested.
"""
import math

import numpy as np
from scipy.interpolate import BSpline
from shapely.geometry import LineString, Point

from .model import Primitive


def tangent_arc(start, end, tangent, net, layer, width):
    start,end,tangent = map(lambda x:np.asarray(x,dtype=float),(start,end,tangent))
    chord=end-start
    normal=np.array([-tangent[1],tangent[0]])
    denominator=2*float(chord@normal)
    if abs(denominator)<1e-9:
        return Primitive(net,layer,width,tuple(start),tuple(end))
    radius=float(chord@chord)/denominator
    center=start+normal*radius
    angle=math.atan2(*(start-center)[::-1])
    end_angle=math.atan2(*(end-center)[::-1])
    sweep=(end_angle-angle)%math.tau if radius>0 else -((angle-end_angle)%math.tau)
    if abs(sweep)>math.pi+1e-6:
        raise ValueError("Biarc would loop")
    middle=center+abs(radius)*np.array([math.cos(angle+sweep/2),math.sin(angle+sweep/2)])
    # KiCad's native collision geometry is poorly conditioned for almost-flat
    # arcs with remote centres. A chord below one degree is visually identical
    # at PCB scale and the adaptive fit still bounds its approximation error.
    if abs(sweep)<math.radians(1) or Point(middle).distance(LineString([start,end]))<1e-5:
        return Primitive(net,layer,width,tuple(start),tuple(end))
    return Primitive(net,layer,width,tuple(start),tuple(end),tuple(middle))


def biarc(start, end, t0, t1, net, layer, width):
    start,end,t0,t1=map(lambda x:np.asarray(x,dtype=float),(start,end,t0,t1))
    v=end-start
    coefficient=1-float(t0@t1)
    b=float(v@(t0+t1))
    c=float(v@v)/2
    if coefficient<1e-10:
        if b<=1e-9:
            raise ValueError("Degenerate biarc")
        distance=c/b
    else:
        # Stable form of the positive quadratic root.
        distance=2*c/(b+math.sqrt(b*b+4*coefficient*c))
    join=(start+end+distance*(t0-t1))/2
    first=tangent_arc(start,join,t0,net,layer,width)
    backwards=tangent_arc(end,join,-t1,net,layer,width)
    second=Primitive(net,layer,width,backwards.end,backwards.start,backwards.mid)
    return [p for p in (first,second) if math.dist(p.start,p.end)>1e-6]


def spline_target(points, pitch):
    line=LineString(points)
    if line.length<1e-5:
        raise ValueError("Zero-length path")
    first=np.array(points[1])-points[0]
    last=np.array(points[-1])-points[-2]
    a=min(pitch*.3,float(np.linalg.norm(first))*.3)
    b=min(pitch*.3,float(np.linalg.norm(last))*.3)
    samples=np.linspace(0,line.length,max(4,math.ceil(line.length/pitch)+1))
    controls=[np.array(points[0]),np.array(points[0])+first/np.linalg.norm(first)*a,
              np.array(points[0])+first/np.linalg.norm(first)*2*a]
    controls.extend(np.array(line.interpolate(t).coords[0]) for t in samples if 2*a<t<line.length-2*b)
    controls.extend([np.array(points[-1])-last/np.linalg.norm(last)*2*b,
                     np.array(points[-1])-last/np.linalg.norm(last)*b,np.array(points[-1])])
    knots=np.r_[np.zeros(4),np.linspace(0,1,len(controls)-2)[1:-1],np.ones(4)]
    return BSpline(knots,np.array(controls),3)


def approximate(spline, net, layer, width, tolerance=.002):
    derivative=spline.derivative()
    knots=np.unique(spline.t)
    def fit(a,b,depth=0):
        p,q=spline(a),spline(b)
        ta,tb=derivative(a),derivative(b)
        ta,tb=ta/np.linalg.norm(ta),tb/np.linalg.norm(tb)
        try:
            arcs=biarc(p,q,ta,tb,net,layer,width)
            if not arcs:
                raise ValueError("Empty fit")
            poly=LineString([point for arc in arcs for point in arc.points()])
            samples=np.unique(np.r_[np.linspace(a,b,17),knots[(knots>a)&(knots<b)]])
            error=max(Point(point).distance(poly) for point in spline(samples))
            if error<=tolerance:
                return arcs
        except ValueError:
            pass
        if depth>=14 or b-a<1e-7:
            raise ValueError("Curve approximation did not converge")
        mid=(a+b)/2
        return fit(a,mid,depth+1)+fit(mid,b,depth+1)
    return fit(0.,1.)


def smooth_path(points, net, layer, width, forbidden, allowed, radius,
                preserve_start=False,preserve_end=False):
    from .router import Scene, fillet
    if len(points)<3:
        return [Primitive(net,layer,width,tuple(points[0]),tuple(points[-1]))]
    # Remove shallow polygonal wiggles without accepting a clearance shortcut.
    first,last = (1 if preserve_start else 0),(-1 if preserve_end else len(points))
    middle=points[first:last]
    simple=list(LineString(middle).simplify(min(.12,width*.3),preserve_topology=False).coords) if len(middle)>1 else middle
    simple=(points[:1] if preserve_start else [])+simple+(points[-1:] if preserve_end else [])
    if Scene.legal(LineString(simple),forbidden,allowed):
        points=simple
    if len(points)<3:
        return [Primitive(net,layer,width,tuple(points[0]),tuple(points[-1]))]
    for pitch in (1.0,.5,.2,.08):
        target=spline_target(points,pitch)
        samples=target(np.linspace(0,1,max(40,math.ceil(LineString(points).length/.08))))
        if not Scene.legal(LineString(samples),forbidden,allowed):
            continue
        try:
            curves=approximate(target,net,layer,width)
        except (ValueError,FloatingPointError):
            continue
        if all(Scene.legal(p.line(),forbidden,allowed) for p in curves):
            return curves
    # Tight corridors retain the previous safe construction rather than moving
    # through copper. The caller's independent/native checks remain mandatory.
    return fillet(points,net,layer,width,forbidden,allowed,radius,angle_threshold=.001)
