"""Add checked tangent shoulders to an already routed KiCad project."""
import argparse
from collections import Counter
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .cli import compare_drc, violation_key
from .model import Board
from .output import board_text, svg
from .policy import via_policy
from .release import Native, copy_project, digest
from .teardrops import TeardropOptions, generate, read_teardrops
from .validate import check


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--kicad-cli", default=os.environ.get("AUTO_HAND_ROUTER_KICAD_CLI", "kicad-cli"))
    parser.add_argument("--kicad-python", default=os.environ.get("AUTO_HAND_ROUTER_KICAD_PYTHON", "/usr/bin/python3"))
    parser.add_argument("--length-ratio", type=float, default=1.0)
    parser.add_argument("--max-length", type=float, default=3.0)
    parser.add_argument("--size", type=float, default=.875, help="Shoulder size, 0 < size <= 1 (default: 12.5%% smaller)")
    parser.add_argument("--via-policy", choices=["courtyards", "module-interiors"], default="courtyards")
    args = parser.parse_args(argv)
    stage = None
    try:
        options = TeardropOptions(args.length_ratio, args.max_length,args.size)
        source, destination = args.board.resolve(), args.output_dir.resolve()
        if destination == source.parent or destination in source.parents:
            raise ValueError("Output must be separate from the source project")
        if destination.exists():
            raise ValueError("Use a fresh output directory for teardrops")
        if "work" not in args.work_dir.resolve().parts:
            raise ValueError("Raw native evidence must be in an ignored work/ directory")
        inputs = [source.with_suffix(ext) for ext in (".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru")]
        hashes = {str(p):digest(p) for p in inputs if p.exists()}
        native = Native(args)
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".ahr-teardrops-", dir=destination.parent))
        candidate = copy_project(source, stage)
        baseline = copy_project(source, native.work/"source")
        before = native.drc(baseline, "before-drc")
        board, modules = via_policy(Board.load(source), args.via_policy)
        primitives, vias = board.original_primitives, board.original_vias
        drops, stats = generate(board, primitives, vias, options)
        candidate.write_text(board_text(board, primitives, vias, drops))
        initial = check(board, Board.load(candidate).expression)
        if not initial["passed"]:
            (native.work/"outline-validation.json").write_text(json.dumps(initial,indent=2))
            raise RuntimeError("Teardrop outline failed independent geometry validation")
        native.worker("fill", candidate, candidate)
        final, _ = via_policy(Board.load(candidate), args.via_policy)
        validation = check(final, final.expression)
        after = native.drc(candidate, "after-drc")
        comparison = compare_drc(before, after)
        added_parity = Counter(violation_key(v) for v in after.get("schematic_parity", [])) - Counter(
            violation_key(v) for v in before.get("schematic_parity", []))
        if not validation["passed"] or not comparison["passed"] or added_parity:
            (native.work/"failed-validation.json").write_text(json.dumps(
                dict(geometry=validation, native=comparison, new_parity=sum(added_parity.values())), indent=2))
            raise RuntimeError("Teardrop copper failed physical/native DRC or schematic parity; see work directory")
        final_drops = read_teardrops(final.expression, final.nets)
        svg(final, [("Before", primitives, vias, read_teardrops(board.expression,board.nets)), ("Tangent teardrops", final.original_primitives,
             final.original_vias, final_drops)], stage/"comparison.svg", labels=True)
        svg(final, [("Tangent teardrops", final.original_primitives, final.original_vias, final_drops)],
            stage/"routed.svg", labels=True)
        import cairosvg
        for name in ("comparison", "routed"):
            path = stage/(name+".svg")
            path.write_text(path.read_text().replace(
                "Auto-Hand-Router · 555 reference PoC · same placement and design rules",
                "Auto-Hand-Router · "+escape(source.stem)+" · tangent pad/via transitions"))
            cairosvg.svg2png(url=str(stage/(name+".svg")), write_to=str(stage/(name+".png")), output_width=1800)
        if any(digest(p) != sha for p,sha in hashes.items()):
            raise RuntimeError("Source project changed during teardrop generation")
        report = dict(source_sha256=hashes, kicad_version=native.version, teardrops=stats,
                      validation=validation, kicad_drc=comparison, new_schematic_parity=sum(added_parity.values()),
                      via_policy=dict(mode=args.via_policy,allowed_module_interiors=modules),
                      commands=native.commands)
        (stage/"report.json").write_text(json.dumps(report, indent=2)+"\n")
        (stage/".auto-hand-router").write_text("Generated and checked teardrop candidate\n")
        stage.rename(destination)
        stage = None
        print(json.dumps(dict(output=str(destination),teardrops=stats,passed=True),indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        if stage is not None:
            failed = args.work_dir.resolve()/"failed-candidate"
            if failed.parent.exists() and not failed.exists():
                shutil.move(str(stage), failed)
                stage = None
        return 1
    finally:
        if stage is not None:
            shutil.rmtree(stage)


if __name__ == "__main__":
    sys.exit(main())
