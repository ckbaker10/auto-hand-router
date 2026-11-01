"""KiCad SWIG subprocess boundary; uses KiCad's Python ABI, only stdlib imports."""
import argparse
import json
from pathlib import Path

import pcbnew as pcb


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["dsn", "ses", "fill", "prune"])
    parser.add_argument("board")
    parser.add_argument("output")
    parser.add_argument("--session")
    args = parser.parse_args()
    board = pcb.LoadBoard(args.board)
    if args.mode == "dsn":
        # Route every physical connection, never rely on a ground-plane shortcut.
        for zone in list(board.Zones()):
            if not zone.GetIsRuleArea():
                board.Remove(zone)
        if not pcb.ExportSpecctraDSN(board, args.output):
            raise RuntimeError("KiCad DSN export failed")
    elif args.mode == "ses":
        if not pcb.ImportSpecctraSES(board, args.session):
            raise RuntimeError("KiCad SES import failed")
        tracks, vias = [], []
        def point(value):
            return [pcb.ToMM(value.x), pcb.ToMM(value.y)]
        for item in board.GetTracks():
            if isinstance(item, pcb.PCB_VIA):
                vias.append(dict(net=item.GetNetname(), center=point(item.GetPosition()),
                                 diameter=pcb.ToMM(item.GetWidth(pcb.F_Cu)),
                                 drill=pcb.ToMM(item.GetDrillValue())))
            else:
                value = dict(net=item.GetNetname(), layer=item.GetLayerName(),
                             width=pcb.ToMM(item.GetWidth()), start=point(item.GetStart()), end=point(item.GetEnd()))
                if isinstance(item, pcb.PCB_ARC):
                    value["mid"] = point(item.GetMid())
                tracks.append(value)
        Path(args.output).write_text(json.dumps(dict(tracks=tracks, vias=vias), indent=2))
    elif args.mode == "prune":
        findings = json.loads(Path(args.session).read_text())
        ids = {i["uuid"] for v in findings["violations"]
               if v["type"] in ("via_dangling", "track_dangling") for i in v["items"]}
        for item in list(board.GetTracks()):
            if item.m_Uuid.AsString() in ids:
                board.Remove(item)
        pcb.SaveBoard(args.output, board)
    else:
        if any(not z.GetIsRuleArea() for z in board.Zones()):
            if not pcb.ZONE_FILLER(board).Fill(board.Zones()):
                raise RuntimeError("KiCad zone filling failed")
        pcb.SaveBoard(args.output, board)


if __name__ == "__main__":
    main()
