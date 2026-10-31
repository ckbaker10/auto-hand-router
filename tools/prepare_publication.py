"""Publish generated previews and compact evidence; omit local settings and raw logs."""
from pathlib import Path
import json
import sys

import cairosvg

from auto_hand_router.audit import tangency_audit
from auto_hand_router.model import Board
from auto_hand_router.output import svg
from auto_hand_router.policy import via_policy


def main():
    root = Path(__file__).resolve().parents[1]
    images = root / "docs/images"
    images.mkdir(parents=True, exist_ok=True)
    records = {}
    for name in ("drying-oven-compact", "drying-fridge"):
        folder = root / "examples" / name
        report = json.loads((folder / "report.json").read_text())
        mode = report.get("via_policy", {}).get("mode", "courtyards")
        board, modules = via_policy(Board.load(folder / (name + ".kicad_pcb")), mode)
        path = images / (name + ".svg")
        svg(board, [(name + " · 0.1.0", board.original_primitives, board.original_vias)], path, labels=True)
        path.write_text(path.read_text().replace("Auto-Hand-Router · 555 reference PoC · same placement and design rules",
                                               "Auto-Hand-Router 0.1.0 · " + name))
        cairosvg.svg2png(url=str(path),write_to=str(path.with_suffix(".png")))
        validation = report["validation"]
        summary = dict(version=report["version"], status=report["status"],
                       source_sha256={Path(p).name:sha for p,sha in report["source_sha256"].items()},
                       kicad_version=report["kicad_version"],
                       copper={k:validation[k] for k in ("passed","arcs","segments","vias","track_length_mm",
                                                        "unconnected_components","minimum_extra_clearance_mm")},
                       kicad_drc=report["kicad_drc"], project_audit=report["project_audit"],
                       tangency=tangency_audit(board),
                       via_policy=dict(mode=mode,allowed_module_interiors=modules,
                                       pad_hole_and_mechanical_keepouts="always enforced"),
                       schematic_parity=dict(inherited_metadata_findings=report["schematic_parity"],
                                             new_findings=report["new_schematic_parity"]),
                       erc_library_findings=report["erc_findings_by_type"],
                       artifact_sha256={name:sha for name,sha in report["artifact_sha256"].items()
                                        if not name.startswith(("seed/","verification/"))
                                        and name not in ("fp-lib-table","sym-lib-table")
                                        and not name.endswith(".kicad_prl")})
        (folder / "summary.json").write_text(json.dumps(summary,indent=2)+"\n")
        records[name]=summary["copper"]
    comparison = root / "comparison"
    sources=json.loads((comparison / "sources.json").read_text())
    clean=[{k:v for k,v in s.items() if k not in ("checkout","actual_revision","working_tree_clean")}
           for s in sources]
    (comparison / "source-revisions.json").write_text(json.dumps(clean,indent=2)+"\n")
    print(json.dumps(records,indent=2))


if __name__ == "__main__":
    sys.exit(main())
