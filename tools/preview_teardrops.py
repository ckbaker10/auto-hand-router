"""Draw our transition detail cases; no external image assets are copied."""
from html import escape
from pathlib import Path

from shapely.geometry import Point, box

from auto_hand_router.curves import biarc
from auto_hand_router.model import Board, Pad, Primitive, Rules, Via
from auto_hand_router.output import polygon_svg, primitive_svg
from auto_hand_router.sexpr import parse
from auto_hand_router.teardrops import generate


def main():
    root = Path(__file__).resolve().parents[1]
    output = root/"docs/images/teardrop-details.svg"
    output.parent.mkdir(parents=True, exist_ok=True)
    cases = [
        ("Gerade Zuführung", [Primitive(1,0,.381,(3,4),(9,4))], []),
        ("Versetzter Eintritt", [Primitive(1,0,.381,(3,4.5),(9,4.5))], []),
        ("Bogen direkt am Pad", biarc((3,4),(6,7),(1,0),(0,1),1,0,.381), []),
        ("Mehrere kurze Stücke", [Primitive(1,0,.381,a,b) for a,b in zip(
            [(3,4),(4.1,4),(4.2,4),(4.4,4),(5,4)],[(4.1,4),(4.2,4),(4.4,4),(5,4),(9,5)])], []),
        ("Kurzer Anschluss vor Verzweigung", [Primitive(1,0,.381,(3,4),(4.8,4)),
             Primitive(1,0,.381,(4.8,4),(8,2)),Primitive(1,0,.381,(4.8,4),(8,6))], []),
        ("Via im durchgehenden Leiterzug", [Primitive(1,0,.381,(1,4),(9,4))], [Via(1,(5,4),1.5,.5)]),
    ]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1440" height="1050" viewBox="0 0 1440 1050">',
             '<rect width="1440" height="1050" fill="#152a2b"/>',
             '<style>text{font:17px sans-serif;fill:#ebf5e9}.title{font-size:25px;font-weight:bold}</style>',
             '<text x="30" y="42" class="title">Auto-Hand-Router · weiche Pad- und Via-Übergänge</text>']
    for index, (name, tracks, vias) in enumerate(cases):
        x, y = (index%2)*720, 65+(index//2)*325
        parts.append(f'<rect x="{x+12}" y="{y}" width="696" height="310" rx="12" fill="#213e3c"/>')
        parts.append(f'<text x="{x+30}" y="{y+30}">{escape(name)}</text>')
        b = Board(Path("in"),Path("pro"),'(kicad_pcb (net 0 "") (net 1 "A"))',[],
                  [] if vias else [Pad("J1","1",1,(3,4),(0,),Point(3,4).buffer(1,quad_segs=64),.5)],
                  box(0,0,11,9),{0:"",1:"A"},{0:Rules(),1:Rules()},[],[])
        b.expression = parse(b.text)
        drops, _ = generate(b,tracks,vias)
        for col, show in enumerate((False,True)):
            px = x+25+col*345
            parts.append(f'<text x="{px}" y="{y+60}">{"mit Teardrops" if show else "vorher"}</text>')
            parts.append(f'<g transform="translate({px},{y+65}) scale(30)">')
            if show:
                for drop in drops:
                    parts.append(f'<polygon points="{polygon_svg(drop.shape)}" fill="#e1bf70"/>')
            for p in tracks:
                parts.append(f'<path d="{primitive_svg(p)}" stroke="#e1bf70" stroke-width="{p.width}" fill="none" stroke-linecap="round"/>')
            for pad in b.pads:
                parts.append(f'<polygon points="{polygon_svg(pad.shape)}" fill="#e1bf70"/>')
                parts.append(f'<circle cx="{pad.center[0]}" cy="{pad.center[1]}" r="{pad.drill/2}" fill="#213e3c"/>')
            for v in vias:
                parts.append(f'<circle cx="{v.center[0]}" cy="{v.center[1]}" r="{v.diameter/2}" fill="#e1bf70"/>')
                parts.append(f'<circle cx="{v.center[0]}" cy="{v.center[1]}" r="{v.drill/2}" fill="#213e3c"/>')
            parts.append('</g>')
    parts.append('</svg>')
    output.write_text('\n'.join(parts))
    import cairosvg
    cairosvg.svg2png(url=str(output),write_to=str(output.with_suffix('.png')))
    print(output)


if __name__ == "__main__":
    main()
