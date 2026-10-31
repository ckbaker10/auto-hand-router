"""Create a before/after crop from actual native copper, not edited screenshots."""
import argparse
from html import escape
import json
from pathlib import Path
import shutil
import subprocess

import cairosvg
from shapely.geometry import Point, box

from auto_hand_router.model import Board
from auto_hand_router.output import polygon_svg, primitive_svg


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir",type=Path,required=True)
    parser.add_argument("--baseline",default="2b1bfda")
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    args.work_dir.mkdir(parents=True,exist_ok=True)
    choices=[]
    for name in ("drying-oven-compact","drying-fridge"):
        current=Board.load(root/"examples"/name/(name+".kicad_pcb"))
        for p in current.original_primitives:
            if p.mid is not None or p.width < current.rules[p.net].width*1.4:
                continue
            pad=next((q for q in current.pads if q.net==p.net and q.shape.covers(Point(p.start))),None)
            if pad is not None:
                choices.append((int(bool(pad.drill))*10+p.line().length,name,current,p,pad))
    _,name,current,stem,pad=max(choices,key=lambda p:p[0])
    folder=args.work_dir/name
    folder.mkdir(exist_ok=True)
    source=root/"examples"/name/(name+".kicad_pcb")
    old=folder/source.name
    old.write_bytes(subprocess.check_output(["git","show",args.baseline+":"+str(source.relative_to(root))],cwd=root))
    shutil.copyfile(source.with_suffix(".kicad_pro"),old.with_suffix(".kicad_pro"))
    before=Board.load(old)
    x,y=pad.center
    view=box(x-5.5,y-4,x+5.5,y+4)
    color="#ba5735" if stem.layer==0 else "#347d89"
    parts=['<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="440" viewBox="0 0 1000 440">',
           '<rect width="100%" height="100%" fill="#f0f3f6"/>',
           '<style>text{font-family:Arial,sans-serif;fill:#172735;font-size:18px}</style>',
           f'<text x="20" y="25">{escape(name+" · "+pad.ref+"."+pad.number+" · "+current.nets[stem.net])}</text>']
    for i,(title,board) in enumerate([("Vorher",before),("Nachher: gemeinsamer breiterer Stamm",current)]):
        parts.append(f'<text x="{20+i*500}" y="58">{title}</text>')
        parts.append(f'<svg x="{20+i*500}" y="75" width="460" height="340" viewBox="{x-5.5} {y-4} 11 8">')
        parts.append(f'<polygon points="{polygon_svg(board.boundary)}" fill="#fbf8eb"/>')
        for p in board.original_primitives:
            if p.layer==stem.layer and p.line().intersects(view):
                parts.append(f'<path d="{primitive_svg(p)}" fill="none" stroke="{color}" stroke-width="{p.width}" stroke-linecap="round" stroke-linejoin="round"/>')
        for p in board.pads:
            if p.shape.intersects(view) and stem.layer in p.layers:
                if p.plated:
                    parts.append(f'<polygon points="{polygon_svg(p.shape)}" fill="{color}"/>')
                if p.drill:
                    parts.append(f'<polygon points="{polygon_svg(p.hole())}" fill="#eef2f5"/>')
        for v in board.original_vias:
            if view.distance(Point(v.center))<v.diameter:
                parts.append(f'<circle cx="{v.center[0]}" cy="{v.center[1]}" r="{v.diameter/2}" fill="{color}"/>')
                parts.append(f'<circle cx="{v.center[0]}" cy="{v.center[1]}" r="{v.drill/2}" fill="#eef2f5"/>')
        parts.append('</svg>')
    parts.append('</svg>')
    target=root/'docs/images/joins-before-after.svg'
    target.write_text('\n'.join(parts))
    cairosvg.svg2png(url=str(target),write_to=str(target.with_suffix('.png')))
    info=dict(board=name,pad=pad.ref+'.'+pad.number,net=current.nets[stem.net],layer=stem.layer,
              baseline=args.baseline,nominal_width_mm=current.rules[stem.net].width,stem_width_mm=stem.width)
    target.with_suffix('.json').write_text(json.dumps(info,indent=2)+'\n')
    print(json.dumps(info))


if __name__=='__main__':
    main()
