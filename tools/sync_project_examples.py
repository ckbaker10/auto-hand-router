"""Import audited native projects and render examples without copying local config."""
import argparse
from html import escape
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

import cairosvg

from auto_hand_router import __version__
from auto_hand_router.model import Board
from auto_hand_router.output import svg
from auto_hand_router.policy import via_policy
from auto_hand_router.teardrops import read_teardrops

NATIVE_EXTENSIONS = (".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def approved_snapshot(project):
    project = Path(project).resolve()
    if project.suffix not in (".kicad_pcb", ".kicad_pro"):
        raise ValueError("Specify the native PCB or project file")
    audit = project.parent/"verification/auto-hand-router-audit.json"
    audit_bytes = audit.read_bytes()
    report = json.loads(audit_bytes)
    if (not report.get("status", "").startswith("PASS")
            or not report.get("copper", {}).get("passed")
            or not report.get("kicad_drc", {}).get("passed")
            or not report.get("project_audit", {}).get("passed")
            or report.get("schematic_parity", {}).get("new_findings") != 0
            or report.get("erc", {}).get("electrical_errors") != 0
            or report.get("tangency", {}).get("free_kinks") != 0
            or not report.get("track_geometry_unchanged")
            or not 0 < report.get("teardrops", {}).get("size", 0) <= 1):
        raise ValueError("The source project needs a passing current routing/teardrop audit")
    snapshots = {}
    for ext in NATIVE_EXTENSIONS:
        file = project.with_suffix(ext)
        data = file.read_bytes()
        if sha(data) != report.get("artifact_sha256", {}).get(file.name):
            raise ValueError("Source differs from its approved audit: "+file.name)
        snapshots[file.name] = data
    return project.with_suffix(".kicad_pcb"), audit, audit_bytes, report, snapshots


def sync_project(project, destination, images, source_revision, before=None):
    if not re.fullmatch(r"[0-9a-f]{7,40}", source_revision):
        raise ValueError("Record the source Git commit, using 7–40 lowercase hexadecimal digits")
    project, audit, audit_bytes, report, snapshots = approved_snapshot(project)
    destination, images = Path(destination).resolve(), Path(images).resolve()
    if destination == project.parent or destination in project.parents:
        raise ValueError("Example output must be separate from the source project")
    backup = destination.with_name(destination.name+".previous")
    if backup.exists():
        raise ValueError("Previous example backup already exists")
    prior = None
    if before is not None:
        before = Path(before).resolve()
        for ext in NATIVE_EXTENSIONS:
            file = before.with_suffix(ext)
            if sha(file.read_bytes()) != report["source_sha256"].get(file.name):
                raise ValueError("Before image does not match the audited source: "+file.name)
        prior, _ = via_policy(Board.load(before), report["via_policy"]["mode"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".auto-hand-router-stage-", dir=destination.parent))
    try:
        for name, data in snapshots.items():
            (stage/name).write_bytes(data)
        (stage/".auto-hand-router").write_text("Auto-Hand-Router checked project snapshot\n")
        board, _ = via_policy(Board.load(stage/project.name), report["via_policy"]["mode"])
        minx,miny,maxx,maxy=board.boundary.bounds
        dimensions=dict(width=maxx-minx,length=maxy-miny)
        if any(abs(value-report["board_mm"][axis]) > 1e-6 for axis,value in dimensions.items()):
            raise ValueError("Native board dimensions differ from the approved audit")
        counts=dict(arcs=sum(p.mid is not None for p in board.original_primitives),
                    segments=sum(p.mid is None for p in board.original_primitives),vias=len(board.original_vias))
        if any(value != report["copper"][kind] for kind,value in counts.items()):
            raise ValueError("Native copper counts differ from the approved audit")
        drops = read_teardrops(board.expression,board.nets)
        if len(drops) != report["teardrops"]["generated"]:
            raise ValueError("Exported zone count differs from the approved audit")
        current = (project.stem+" · size "+str(report["teardrops"]["size"]),
                   board.original_primitives,board.original_vias,drops)
        panels = [] if prior is None else [("Before teardrops",prior.original_primitives,
                                          prior.original_vias,read_teardrops(prior.expression,prior.nets))]
        panels.append(current)
        for name, groups in (("routed",[current]), ("comparison",panels)):
            path = stage/(name+".svg")
            svg(board,groups,path,labels=True)
            path.write_text(path.read_text().replace(
                "Auto-Hand-Router · 555 reference PoC · same placement and design rules",
                "Auto-Hand-Router · "+escape(project.stem)+" · current checked design"))
            cairosvg.svg2png(url=str(path),write_to=str(stage/(name+".png")),output_width=1800)
        summary = dict(version=__version__,status=report["status"],source_revision=source_revision,
                       source_audit_sha256=sha(audit_bytes),source_sha256=report["source_sha256"],
                       kicad_version=report["kicad_version"],router_commit=report["router_commit"],
                       board_mm=report["board_mm"],
                       copper={k:report["copper"][k] for k in ("passed","arcs","segments","vias",
                           "teardrops","track_length_mm","unconnected_components","minimum_extra_clearance_mm")},
                       teardrops=report["teardrops"],kicad_drc=report["kicad_drc"],
                       project_audit=report["project_audit"],tangency=report["tangency"],
                       via_policy=report["via_policy"],schematic_parity=report["schematic_parity"],
                       erc_library_findings=report["erc"].get("findings",{}),
                       track_geometry_unchanged=report["track_geometry_unchanged"],
                       artifact_sha256={f.name:sha(f.read_bytes()) for f in stage.iterdir() if f.is_file()})
        (stage/"summary.json").write_text(json.dumps(summary,indent=2)+"\n")
        # Rendering may take time: recheck all four inputs and their approval
        # before replacing a previous example or its public preview.
        if audit.read_bytes() != audit_bytes or any((project.parent/name).read_bytes() != data
                                                   for name,data in snapshots.items()):
            raise ValueError("Source project changed during example rendering")
        if destination.exists():
            destination.rename(backup)
        try:
            stage.rename(destination)
            stage = None
        except BaseException:
            if backup.exists():
                backup.rename(destination)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        images.mkdir(parents=True, exist_ok=True)
        for extension in (".svg", ".png"):
            shutil.copyfile(destination/("routed"+extension),images/(project.stem+extension))
            shutil.copyfile(destination/("comparison"+extension),images/(project.stem+"-teardrops"+extension))
        return summary
    finally:
        if stage is not None:
            shutil.rmtree(stage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project",type=Path,required=True)
    parser.add_argument("--source-revision",required=True)
    parser.add_argument("--before-board",type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    result = sync_project(args.project,root/"examples"/args.project.stem,
                          root/"docs/images",args.source_revision,args.before_board)
    print(json.dumps(dict(project=args.project.stem,source_revision=result["source_revision"],
                         board_mm=result["board_mm"],teardrops=result["teardrops"],passed=True),indent=2))


if __name__ == "__main__":
    main()
