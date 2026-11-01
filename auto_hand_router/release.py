"""Version 0.1 production candidate pipeline: negotiated seed and spacious curves."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import __version__
from .audit import project_audit, tangency_audit
from .cli import cli_environment, compare_drc, violation_key
from .finishing import finish, import_copper
from .model import EPS, Board
from .output import board_text, svg
from .policy import via_policy
from .repair import repair, ripup_repair
from .router import Options
from .sexpr import parse
from .validate import check
from .teardrops import TeardropOptions, generate as generate_teardrops, read_teardrops


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def copy_project(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    for extension in (".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru"):
        p = source.with_suffix(extension)
        if p.exists():
            shutil.copyfile(p, destination / p.name)
    for name in ("fp-lib-table", "sym-lib-table"):
        p = source.parent / name
        if p.exists():
            (destination / name).write_text(p.read_text().replace("${KIPRJMOD}", str(source.parent)))
    return destination / source.name


def profile_project(board_path, profile_path, routing_config):
    if profile_path is None:
        return None
    profile = json.loads(profile_path.read_text())
    data = json.loads(board_path.with_suffix(".kicad_pro").read_text())
    limits = profile["limits_mm"]["kicad_rules"]
    rules = data["board"]["design_settings"]["rules"]
    for key, value in limits.items():
        rules[key] = max(rules.get(key, value), value) if key.startswith("min_") else value
    routing = profile["routing"]
    classes = data["net_settings"]["classes"]
    default = next(c for c in classes if c["name"] == "Default")
    for c in classes:
        for key, value in dict(clearance=routing["clearance_mm"],
                               track_width=routing["signal_width_mm"],
                               via_diameter=routing["via_diameter_mm"], via_drill=routing["via_drill_mm"]).items():
            c[key] = max(c.get(key, 0), value)
    power = next((c for c in classes if c["name"] == "Power"), None)
    if power is None:
        power = dict(default, name="Power")
        classes.append(power)
    power["track_width"] = max(power["track_width"], routing["power_width_mm"])
    patterns = data["net_settings"].get("netclass_patterns") or []
    for pattern in routing_config.get("routing", {}).get("power_net_patterns", []):
        assignment = dict(netclass="Power", pattern=pattern)
        if assignment not in patterns:
            patterns.append(assignment)
    data["net_settings"]["netclass_patterns"] = patterns
    board_path.with_suffix(".kicad_pro").write_text(json.dumps(data, indent=2) + "\n")
    return dict(path=str(profile_path), sha256=digest(profile_path), name=profile["name"],
                applied_to_candidate_only=True)


class Native:
    def __init__(self, args):
        cli = shutil.which(args.kicad_cli) if "/" not in args.kicad_cli else args.kicad_cli
        if cli is None:
            raise ValueError("KiCad CLI was not found")
        self.cli = Path(cli).resolve()
        self.python = args.kicad_python
        self.work = args.work_dir.resolve()
        if self.work.exists() and any(self.work.iterdir()):
            raise ValueError("Use a fresh work directory for every routing run")
        self.work.mkdir(parents=True, exist_ok=True)
        self.env = cli_environment(self.cli, self.work)
        prefix = self.cli.parent.parent
        sdk = prefix / "lib/python3/dist-packages"
        if sdk.exists():
            self.env["PYTHONPATH"] = str(sdk) + os.pathsep + self.env.get("PYTHONPATH", "")
        self.commands = []
        version = self.run([str(self.cli), "version"], "kicad-version").strip()
        if not re.match(r"10\.", version):
            raise ValueError("KiCad 10 is required; found " + version)
        self.version = version

    def run(self, command, label, timeout=180, accepted=(0,), env=None):
        start = time.time()
        stdout = self.work / (label + ".stdout.log")
        stderr = self.work / (label + ".stderr.log")
        with stdout.open("w") as out, stderr.open("w") as err:
            process = subprocess.Popen(list(map(str, command)), env=env or self.env, stdout=out, stderr=err)
            (self.work / (label + ".pid")).write_text(str(process.pid))
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
        self.commands.append(dict(argv=list(map(str, command)), label=label, exit_code=returncode,
                                  started_epoch=start, finished_epoch=time.time()))
        (self.work / "commands.json").write_text(json.dumps(self.commands, indent=2))
        if returncode not in accepted:
            raise RuntimeError(f"{label} failed ({returncode}); see {stderr}")
        return stdout.read_text()

    def worker(self, mode, board, output, session=None):
        command = [self.python, Path(__file__).with_name("native_worker.py"), mode, board, output]
        if session is not None:
            command.extend(["--session", session])
        self.run(command, mode + "-" + Path(output).stem)

    def drc(self, board, label):
        output = self.work / (label + ".json")
        parity = ["--schematic-parity"] if Path(board).with_suffix(".kicad_sch").exists() else []
        self.run([self.cli, "pcb", "drc", *parity, "--all-track-errors",
                  "--severity-all", "--exit-code-violations", "--format", "json", "-o", output, board],
                 label, accepted=(0, 5))
        return json.loads(output.read_text())


def constrained_dsn(board, source, destination):
    """Keep native rules, add conservative via bodies and numerical headroom."""
    text = source.read_text()
    if "(unit um)" not in text or text.count("  (structure\n") != 1:
        raise ValueError("Expected KiCad DSN in micrometres")
    clearance = max(r.clearance for r in board.rules.values())
    # Include explicit numerical headroom; no original clearance is decreased.
    text = re.sub(r"\(clearance ([0-9.]+)", lambda m: "(clearance " +
                  f"{max(float(m[1]), (clearance + 2*EPS)*1000):.4f}", text)
    additions = []
    def polygon(kind, name, shape):
        for i, area in enumerate(getattr(shape, "geoms", [shape])):
            if area.geom_type != "Polygon":
                continue
            coords = " ".join(f"{x*1000:.4f} {-y*1000:.4f}" for x, y in area.exterior.coords)
            additions.append(f'    ({kind} "ahr_{name}_{i}" (polygon signal 0 {coords}))')
    for i, (_, area) in enumerate(board.via_keepouts):
        polygon("via_keepout", f"body_{i}", area.buffer(clearance + 2*EPS))
    for i, pad in enumerate(board.pads):
        if pad.drill:
            polygon("via_keepout", f"hole_{i}", pad.hole().buffer(
                max(r.hole_to_hole for r in board.rules.values()) + 2*EPS))
        if pad.drill and not pad.plated:
            polygon("keepout", f"npth_{i}", pad.hole().buffer(
                max(r.hole for r in board.rules.values()) + 2*EPS))
    destination.write_text(text.replace("  (structure\n", "  (structure\n" + "\n".join(additions) + "\n", 1))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--kicad-cli", default=os.environ.get("AUTO_HAND_ROUTER_KICAD_CLI", "kicad-cli"))
    parser.add_argument("--kicad-python", default=os.environ.get("AUTO_HAND_ROUTER_KICAD_PYTHON", "/usr/bin/python3"))
    parser.add_argument("--freerouting-jar", type=Path,
                        default=os.environ.get("AUTO_HAND_ROUTER_FREEROUTING_JAR"))
    parser.add_argument("--java", default=os.environ.get("AUTO_HAND_ROUTER_JAVA", "java"))
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--routing-config", type=Path)
    parser.add_argument("--spacing", type=float, default=1.2)
    parser.add_argument("--radius", type=float, default=2.5)
    parser.add_argument("--no-teardrops", action="store_true", help="Leave pad/via shoulders unchanged")
    parser.add_argument("--teardrop-length-ratio", type=float, default=1.0)
    parser.add_argument("--teardrop-max-length", type=float, default=3.0, help="Maximum shoulder length outside the pad, mm")
    parser.add_argument("--teardrop-size", type=float, default=.875, help="Shoulder size, 0 < size <= 1")
    parser.add_argument("--relax-steps", type=int, default=30)
    parser.add_argument("--route-timeout", type=int, default=600)
    parser.add_argument("--seed-session", type=Path, help="Validate and continue an existing Specctra session")
    parser.add_argument("--seed-board", type=Path, help="Validate and continue an existing routed copper checkpoint")
    parser.add_argument("--backend", choices=["freerouting", "kreisel"], default="freerouting")
    parser.add_argument("--via-policy", choices=["courtyards", "module-interiors"], default="courtyards")
    args = parser.parse_args(argv)
    stage = None
    native = None
    try:
        if args.spacing < 0 or args.radius <= 0 or args.relax_steps < 0:
            raise ValueError("Spacing/relaxation must be nonnegative and radius positive")
        drop_options = TeardropOptions(args.teardrop_length_ratio, args.teardrop_max_length,args.teardrop_size)
        source = args.board.resolve()
        destination = args.output_dir.resolve()
        if destination == source.parent or destination in source.parents:
            raise ValueError("Output must be separate from the source project")
        if destination.exists() and any(destination.iterdir()) and not (destination / ".auto-hand-router").exists():
            raise ValueError("Refusing to replace a non-owned output directory")
        sources = {str(p): digest(p) for p in [source.with_suffix(ext) for ext in
                   (".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru")] if p.exists()}
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".ahr-", dir=destination.parent))
        (stage / ".auto-hand-router").write_text("0.1.0\n")
        native = Native(args)
        jar = args.freerouting_jar.resolve() if args.freerouting_jar else None
        if args.backend == "freerouting" and not (args.seed_board or args.seed_session) and jar is None:
            raise ValueError("Freerouting requires --freerouting-jar or AUTO_HAND_ROUTER_FREEROUTING_JAR")
        if jar is not None and digest(jar) != "f6f51bb02245e8e717f9359bd260cc9c5c0b1bc0acc8b7cb2cd5b8ffeb5de3c7":
            raise ValueError("Expected the pinned official Freerouting 2.5.0 JAR")
        original = copy_project(source, native.work / "source")
        original_drc = native.drc(original, "source-drc")
        candidate = copy_project(source, stage)
        config_path = args.routing_config or source.parent / "kicad-workflow.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        profile = profile_project(candidate, args.profile, config)
        board, allowed_modules = via_policy(Board.load(candidate), args.via_policy)
        baseline_drc = native.drc(candidate, "profile-baseline-drc")
        netlist = stage / "verification/carrier.net.xml"
        netlist.parent.mkdir()
        native.run([native.cli, "sch", "export", "netlist", "--format", "kicadxml", "-o", netlist,
                    candidate.with_suffix(".kicad_sch")], "netlist")
        erc_path = native.work / "erc.json"
        native.run([native.cli, "sch", "erc", "--exit-code-violations", "--format", "json", "-o", erc_path,
                    candidate.with_suffix(".kicad_sch")], "erc", accepted=(0, 5))
        erc = json.loads(erc_path.read_text())
        erc_findings = [v for s in erc["sheets"] for v in s["violations"]]
        if any(v["severity"] == "error" or v["type"] not in
               ("lib_symbol_issues", "footprint_link_issues") for v in erc_findings):
            raise RuntimeError("Source schematic ERC is not clean; see " + str(erc_path))
        candidate.write_text(board_text(board))
        raw_dsn, dsn, session = [native.work / name for name in ("native.dsn", "constrained.dsn", "routed.ses")]
        native.worker("dsn", candidate, raw_dsn)
        constrained_dsn(board, raw_dsn, dsn)
        java = str(Path(args.java).resolve()) if "/" in args.java else args.java
        command = [java, "-Xmx2g", "-XX:ActiveProcessorCount=3", "-Djava.awt.headless=true", "-jar", jar,
                   "-de", dsn, "-do", session, "--gui.enabled=false", "--api_server.enabled=false",
                   "--mcp_server.enabled=false", "--usage_and_diagnostic_data.disable_analytics=true",
                   "--logging.file.enabled=false", "--router.autorouter.max_passes=100",
                   "--router.optimizer.max_threads=2", "--router.automatic_neckdown=false",
                   f"--router.copper_to_edge_clearance_um={1000*(max(r.edge for r in board.rules.values())+2*EPS):.3f}",
                   f"--router.hole_clearance_um={1000*(max(r.hole for r in board.rules.values())+2*EPS):.3f}",
                   f"--user_data_path={native.work / 'freerouting-user'}"]
        print(f"Routing seed using {args.backend}; constraints and logs:", native.work, flush=True)
        if args.seed_session and args.seed_board:
            raise ValueError("Choose one seed input")
        backend = dict(name="Freerouting",version="2.5.0",jar_sha256=digest(jar) if jar else None)
        if args.seed_board or args.seed_session:
            backend = dict(name="Imported validated copper checkpoint" if args.seed_board else "Imported Specctra session",
                           source_sha256=digest(args.seed_board or args.seed_session))
        if args.backend == "kreisel" and not args.seed_board and not args.seed_session:
            from .kreisel_backend import route as core_route
            seed_primitives, vias, backend = core_route(board)
            data_path = None
        elif args.seed_board:
            checkpoint = Board.load(args.seed_board)
            project_audit(Board.load(original), checkpoint, netlist)
            from .model import LAYERS
            data = dict(tracks=[dict(net=checkpoint.nets[p.net], layer=LAYERS[p.layer], width=p.width,
                                     start=p.start, end=p.end, mid=p.mid) for p in checkpoint.original_primitives],
                        vias=[dict(net=checkpoint.nets[v.net], center=v.center, diameter=v.diameter,
                                   drill=v.drill) for v in checkpoint.original_vias])
        elif args.seed_session:
            shutil.copyfile(args.seed_session, session)
        else:
            native.run(command, "freerouting", timeout=args.route_timeout)
        if args.backend != "kreisel" or args.seed_board or args.seed_session:
            data_path = native.work / "seed.json"
            if args.seed_board:
                data_path.write_text(json.dumps(data))
            else:
                native.worker("ses", candidate, data_path, session=session)
            seed_primitives, vias = import_copper(board, json.loads(data_path.read_text()))
        seed_validation = check(board, parse(board_text(board, seed_primitives, vias)))
        (native.work / "seed-validation.json").write_text(json.dumps(seed_validation, indent=2))
        repairs = []
        checkpoint_path = copy_project(candidate, native.work / "checkpoints")
        def save_checkpoint(copper, barrels):
            checkpoint_path.write_text(board_text(board, copper, barrels))
        save_checkpoint(seed_primitives, vias)
        if not seed_validation["geometry_errors"] and seed_validation["unconnected_components"]:
            repair_options = Options(grid=.25, spacing=.4, max_expansions=600_000)
            def progress(msg):
                print(msg, flush=True)
            try:
                seed_primitives, vias, repairs = repair(board, seed_primitives, vias,
                    repair_options, progress=progress, checkpoint=save_checkpoint)
            except RuntimeError:
                seed_primitives, vias, repairs = ripup_repair(board, seed_primitives, vias,
                    repair_options, progress=progress, checkpoint=save_checkpoint)
            seed_validation = check(board, parse(board_text(board, seed_primitives, vias)))
        if not seed_validation["passed"]:
            raise RuntimeError("Seed failed physical checks; see " + str(native.work / "seed-validation.json"))
        seed_board = copy_project(candidate, stage / "seed")
        seed_board.write_text(board_text(board, seed_primitives, vias))
        options = Options(spacing=args.spacing, radius=args.radius, relax_steps=args.relax_steps)
        primitives, vias, shaping = finish(board, seed_primitives, vias, options,
                                           progress=lambda msg: print(msg, flush=True))
        drops, drop_stats = ([], dict(generated=0, disabled=True)) if args.no_teardrops else generate_teardrops(
            board, primitives, vias, drop_options)
        candidate.write_text(board_text(board, primitives, vias, drops))
        shaped_validation = check(board, parse(candidate.read_text()))
        if not shaped_validation["passed"]:
            (native.work / "shaping-validation.json").write_text(json.dumps(shaped_validation, indent=2))
            raise RuntimeError("Shaping failed physical checks; see " + str(native.work / "shaping-validation.json"))
        native.worker("fill", candidate, candidate)
        final, _ = via_policy(Board.load(candidate), args.via_policy)
        validation = check(final, final.expression)
        routed_drc = native.drc(candidate, "routed-drc")
        pruned = []
        for iteration in range(12):
            if any(v["severity"]=="error" for v in routed_drc["violations"]):
                break
            dangling = [v for v in routed_drc["violations"] if v["type"] in ("via_dangling", "track_dangling")]
            if not dangling:
                break
            findings_path = native.work / "prune-findings.json"
            findings_path.write_text(json.dumps(routed_drc))
            native.worker("prune", candidate, candidate, session=findings_path)
            final, _ = via_policy(Board.load(candidate), args.via_policy)
            validation = check(final, final.expression)
            if not validation["passed"]:
                raise RuntimeError("Removing dangling copper damaged physical connectivity")
            pruned.append(dict(iteration=iteration, findings=len(dangling)))
            routed_drc = native.drc(candidate, "pruned-drc-" + str(iteration))
        (native.work / "final-validation.json").write_text(json.dumps(validation, indent=2))
        project_checks = project_audit(Board.load(original), final, netlist)
        tangency = tangency_audit(final)
        native_comparison = compare_drc(baseline_drc, routed_drc)
        before_parity = Counter(violation_key(v) for v in baseline_drc.get("schematic_parity", []))
        after_parity = Counter(violation_key(v) for v in routed_drc.get("schematic_parity", []))
        added_parity = after_parity - before_parity
        if not validation["passed"] or not native_comparison["passed"] or added_parity:
            raise RuntimeError("Final KiCad/physical/parity validation failed; see " + str(native.work))
        final_drops = read_teardrops(final.expression, final.nets)
        svg(board, [("Freerouting seed", seed_primitives, vias),
                    ("Spacious curved 0.1.0", final.original_primitives, final.original_vias, final_drops)], stage / "comparison.svg", labels=True)
        svg(final, [("Spacious curved 0.1.0", final.original_primitives, final.original_vias, final_drops)], stage / "routed.svg", labels=True)
        import cairosvg
        for name in ("comparison", "routed"):
            path = stage / (name + ".svg")
            path.write_text(path.read_text().replace("Auto-Hand-Router · 555 reference PoC · same placement and design rules",
                                                    "Auto-Hand-Router 0.1.0 · " + source.stem))
            cairosvg.svg2png(url=str(stage / (name + ".svg")), write_to=str(stage / (name + ".png")))
        for label, value in [("erc", erc), ("drc-parity", routed_drc)]:
            (stage / "verification" / (label + ".json")).write_text(json.dumps(value, indent=2))
        if any(digest(path) != sha for path, sha in sources.items()):
            raise RuntimeError("Source project changed during routing; candidate was not published")
        report = dict(version=__version__, status="validated routing candidate; DRAFT hardware",
                      source_sha256=sources, kicad_version=native.version,
                      backend=backend,
                      profile=profile, effective_rules={board.nets[n]:vars(r) for n,r in board.rules.items()},
                      seed_validation=seed_validation, validation=validation, shaping=shaping,
                      repairs=repairs, teardrops=drop_stats,
                      project_audit=project_checks, tangency=tangency,
                      pruned_dangling=pruned,
                      via_policy=dict(mode=args.via_policy, allowed_module_interiors=allowed_modules,
                                      pad_hole_and_mechanical_keepouts="always enforced"),
                      imported_session_sha256=digest(args.seed_session) if args.seed_session else None,
                      imported_board_sha256=digest(args.seed_board) if args.seed_board else None,
                      kicad_drc=native_comparison, source_drc_counts=dict(Counter(v["type"] for v in original_drc["violations"])),
                      schematic_parity=len(routed_drc.get("schematic_parity", [])),
                      new_schematic_parity=sum(added_parity.values()),
                      erc_findings_by_type=dict(Counter(v["type"] for v in erc_findings)),
                      evidence_dir=str(native.work), commands=native.commands,
                      artifact_sha256={str(p.relative_to(stage)):digest(p) for p in stage.rglob("*") if p.is_file()})
        (stage / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        backup = destination.with_name(destination.name + ".previous")
        if backup.exists():
            raise ValueError("Previous output backup exists: " + str(backup))
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
        print(json.dumps(dict(output=str(destination), passed=True, arcs=validation["arcs"],
                              vias=validation["vias"], unconnected=validation["unconnected_components"]), indent=2))
        return 0
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        if stage is not None and native is not None:
            # Keep the failed concrete candidate in ignored evidence, never overwrite a good output.
            failed = args.work_dir.resolve() / "failed-candidate"
            if not failed.exists():
                shutil.move(str(stage), failed)
                stage = None
        return 1
    finally:
        if stage is not None:
            shutil.rmtree(stage)
