"""Run with KiCad's Python ABI (3.12); exchange geometry without changing routes."""
import json
from pathlib import Path
import sys

import pcbnew

mode, source, target = sys.argv[1:4]
board = pcbnew.LoadBoard(source)
if mode == "dsn":
    if not pcbnew.ExportSpecctraDSN(board, target):
        raise RuntimeError("DSN export failed")
elif mode == "ses":
    if not pcbnew.ImportSpecctraSES(board, target):
        raise RuntimeError("SES import failed")
    pcbnew.SaveBoard(sys.argv[4], board)
elif mode == "json":
    def point(p):
        return [pcbnew.ToMM(p.x), pcbnew.ToMM(p.y)]
    tracks, vias = [], []
    for item in board.GetTracks():
        net = item.GetNetname()
        if isinstance(item, pcbnew.PCB_VIA):
            vias.append(dict(net=net, center=point(item.GetPosition()),
                             diameter=pcbnew.ToMM(item.GetWidth(pcbnew.F_Cu)),
                             drill=pcbnew.ToMM(item.GetDrillValue())))
        else:
            record = dict(net=net, layer=0 if item.GetLayerName() == "F.Cu" else 1,
                          width=pcbnew.ToMM(item.GetWidth()), start=point(item.GetStart()),
                          end=point(item.GetEnd()))
            if isinstance(item, pcbnew.PCB_ARC):
                record["mid"] = point(item.GetMid())
            tracks.append(record)
    Path(target).write_text(json.dumps(dict(tracks=tracks, vias=vias), indent=2))
else:
    raise ValueError(mode)
