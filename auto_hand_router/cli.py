"""Generate a routed reference board and retain independent validation evidence."""

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__
from .model import Board
from .output import board_text, svg
from .router import Options, route_board
from .sexpr import parse
from .validate import check

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOARD = ROOT / "benchmarks/sources/555-clock/555_Timer_Clock.kicad_pcb"


def cli_environment(binary, evidence):
    env = os.environ.copy()
    # Also permits an isolated unpacked official package; no host library changes.
    prefix = Path(binary).resolve().parent.parent
    lib_dirs = [prefix / "lib/x86_64-linux-gnu", prefix / "lib"]
    env["LD_LIBRARY_PATH"] = ":".join(map(str, lib_dirs)) + ":" + env.get("LD_LIBRARY_PATH", "")
    stock = prefix / "share/kicad"
    if stock.is_dir():
        env["KICAD_STOCK_DATA_HOME"] = str(stock)
    env["XDG_CONFIG_HOME"] = str(evidence / "kicad-config")
    return env


def drc(binary, board, report, env):
    command = [str(binary), "pcb", "drc", "--format", "json", "--severity-all",
               "--all-track-errors", "--exit-code-violations", "--output", str(report), str(board)]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=180)
    report.with_suffix(".stdout.log").write_text(result.stdout)
    report.with_suffix(".stderr.log").write_text(result.stderr)
    if result.returncode not in (0, 5) or not report.is_file():
        raise RuntimeError(f"KiCad DRC failed (exit {result.returncode}); see {report.parent}")
    return json.loads(report.read_text())


def violation_key(v):
    return (v.get("type"), v.get("severity"), v.get("description"),
            tuple(sorted(i.get("uuid", "") for i in v.get("items", []))))


def compare_drc(baseline, routed):
    before = Counter(violation_key(v) for v in baseline.get("violations", []))
    after = Counter(violation_key(v) for v in routed.get("violations", []))
    added = after - before
    unconnected = routed.get("unconnected_items", [])
    return {"passed": not added and not unconnected,
            "unconnected_items": len(unconnected),
            "baseline_violation_count": sum(before.values()),
            "routed_violation_count": sum(after.values()),
            "new_violations": [{"type": k[0], "severity": k[1], "description": k[2], "count": v}
                               for k, v in sorted(added.items(), key=lambda i: str(i[0]))],
            "routed_violation_types": dict(Counter(v.get("type") for v in routed.get("violations", [])))}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "route":
        from .release import main as route_main
        return route_main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--board", type=Path, default=DEFAULT_BOARD)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "examples/555-reference")
    parser.add_argument("--grid", type=float, default=0.20)
    parser.add_argument("--spacing", type=float, default=1.2, help="Preferred extra clearance in mm; not a relaxed design rule")
    parser.add_argument("--comparison", action="store_true", help="Also route an unspread comparison with identical widths/rules")
    parser.add_argument("--kicad-cli", type=Path)
    parser.add_argument("--evidence-dir", type=Path, help="Ignored work/ location for raw KiCad reports")
    args = parser.parse_args(argv)
    stage = None
    try:
        if args.kicad_cli and not args.evidence_dir:
            parser.error("--kicad-cli requires --evidence-dir in ignored work/")
        out = args.output_dir.resolve()
        if out.exists() and any(out.iterdir()) and not (out / ".auto-hand-router").is_file():
            raise ValueError("Output directory is nonempty and not owned by this PoC")
        if out == args.board.resolve().parent:
            raise ValueError("Output must not be the original board directory")
        board = Board.load(args.board, args.project)
        source_hash = hashlib.sha256(board.source.read_bytes()).hexdigest()
        project_hash = hashlib.sha256(board.project.read_bytes()).hexdigest()
        destination = out
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".auto-hand-router-stage-", dir=destination.parent))
        out = stage
        (out / ".auto-hand-router").write_text("Generated reference artifacts; originals are not modified.\n")
        print(f"Routing {board.source.name}: {len(board.pads)} pads, F.Cu/B.Cu", flush=True)
        original = out / "original.kicad_pcb"
        unrouted = out / "unrouted.kicad_pcb"
        routed = out / "routed.kicad_pcb"
        original.write_text(board.text)
        unrouted.write_text(board_text(board))
        options = Options(grid=args.grid, spacing=args.spacing)
        started = time.time()  # host clock, never device time
        routes, stats = route_board(board, options, progress=lambda s: print(s, flush=True))
        primitives = [p for r in routes for p in r.primitives]
        vias = [v for r in routes for v in r.vias]
        routed.write_text(board_text(board, primitives, vias))
        elapsed = time.time() - started
        validation = check(board, parse(routed.read_text()))
        panels = [("Original", board.original_primitives, board.original_vias)]
        report = {"poc_version": __version__, "source": str(board.source), "source_sha256": source_hash,
                  "project_sha256": project_hash, "routing": stats, "routing_host_seconds": elapsed,
                  "effective_rules": {board.nets[net]: vars(rule) for net, rule in board.rules.items() if net},
                  "validation": validation, "kicad_drc": {"status": "not_run"},
                  "via_policy": "No via-in-pad or vias in footprint courtyards, including same-net pads; copper disk plus net clearance outside",
                  "limitations": ["File-based PoC, not an IPC plugin", "No zones, cutouts, custom rules or back-side footprints",
                                  "Only the supported two-layer pad/contour/rule subset is accepted",
                                  "Spacing is an optimization preference; pad escape may stay dense"]}
        outputs = [original, unrouted, routed]
        if args.comparison:
            compact_routes, _ = route_board(board, replace(options, spacing=0, relax_steps=0))
            cp = [p for r in compact_routes for p in r.primitives]
            cv = [v for r in compact_routes for v in r.vias]
            compact = out / "compact.kicad_pcb"
            compact.write_text(board_text(board, cp, cv))
            outputs.append(compact)
            report["compact_validation"] = check(board, parse(compact.read_text()))
            panels.append(("Compact reference", cp, cv))
        panels.append(("Spacious / curved", primitives, vias))
        svg(board, panels, out / "comparison.svg")
        svg(board, [("Spacious / curved", primitives, vias)], out / "routed.svg")
        import cairosvg
        cairosvg.svg2png(url=str(out / "comparison.svg"), write_to=str(out / "comparison.png"), output_width=1800)
        cairosvg.svg2png(url=str(out / "routed.svg"), write_to=str(out / "routed.png"), output_width=900)
        for path in outputs:
            shutil.copyfile(board.project, path.with_suffix(".kicad_pro"))
        source_license = board.source.parent / "LICENSE"
        if source_license.is_file():
            shutil.copyfile(source_license, out / "SOURCE-LICENSE.txt")
        if args.kicad_cli:
            evidence = args.evidence_dir.resolve()
            if "work" not in evidence.parts:
                raise ValueError("Raw DRC evidence must be in work/")
            evidence.mkdir(parents=True, exist_ok=True)
            env = cli_environment(args.kicad_cli, evidence)
            version = subprocess.run([str(args.kicad_cli), "version"], env=env, text=True,
                                     capture_output=True, check=True, timeout=30).stdout.strip()
            if not version.startswith("10."):
                raise RuntimeError(f"Expected KiCad 10, found {version}")
            reports = {path.stem: drc(args.kicad_cli, path, evidence / (path.stem + ".json"), env) for path in outputs}
            report["kicad_drc"] = {"status": "run", "version": version,
                                    "original_unconnected": len(reports["original"].get("unconnected_items", [])),
                                    "unrouted_unconnected": len(reports["unrouted"].get("unconnected_items", [])),
                                    "routed": compare_drc(reports["unrouted"], reports["routed"])}
            if "compact" in reports:
                report["kicad_drc"]["compact"] = compare_drc(reports["unrouted"], reports["compact"])
        if hashlib.sha256(board.source.read_bytes()).hexdigest() != source_hash:
            raise RuntimeError("Original board changed while routing")
        if hashlib.sha256(board.project.read_bytes()).hexdigest() != project_hash:
            raise RuntimeError("Original project changed while routing")
        report["artifact_sha256"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                     for path in sorted(out.iterdir()) if path.is_file()
                                     and path.name != "report.json"}
        passed = validation["passed"] and report.get("compact_validation", {"passed": True})["passed"]
        if args.kicad_cli:
            passed &= report["kicad_drc"]["routed"]["passed"]
            passed &= report["kicad_drc"].get("compact", {"passed": True})["passed"]
        (out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if passed:
            destination.mkdir(parents=True, exist_ok=True)
            # Publish only a completed successful run; report last lets readers verify hashes.
            for path in sorted(out.iterdir(), key=lambda p: (p.name == "report.json", p.name)):
                path.replace(destination / path.name)
        elif args.evidence_dir:
            (args.evidence_dir / "failed-summary.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"output": str(destination), "passed": passed, "segments": validation["segments"],
                          "arcs": validation["arcs"], "vias": validation["vias"],
                          "unconnected": validation["unconnected_components"],
                          "kicad_drc_status": report["kicad_drc"]["status"],
                          "host_seconds": round(elapsed, 2)}, indent=2))
        return 0 if passed else 1
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"PoC failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
