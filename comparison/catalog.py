"""Capture fixed local checkouts, published runtime provenance and artifact hashes."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
comp = Path(sys.argv[1])
research = json.loads((root / "research/sources.json").read_text())
records = []
for entry in research["projects"]:
    folder = comp / "upstream" / entry["id"]
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=folder, text=True).strip()
    if revision != entry["revision"]:
        raise ValueError(f"Unexpected source revision: {folder}")
    records.append(dict(entry, checkout=str(folder), actual_revision=revision,
                        working_tree_clean=not subprocess.check_output(
                            ["git", "status", "--porcelain"], cwd=folder, text=True).strip()))
(root / "comparison/sources.json").write_text(json.dumps(records, indent=2))
provenance = dict(checkouts=records, evidence_root=str(comp), runs={})
for metadata in sorted((comp / "runs").glob("*/metadata.json")):
    data = json.loads(metadata.read_text())
    if data.get("exit_code") is None:
        raise ValueError(f"Unfinished run: {metadata}")
    provenance["runs"][metadata.parent.name] = data
provenance["freerouting_release"] = json.loads((comp / "freerouting-runtime/PROVENANCE.json").read_text())
reference = root / "examples/555-reference"
golden = json.loads((reference / "report.json").read_text())
provenance["accepted_reference_unchanged"] = all(
    hashlib.sha256((reference / f).read_bytes()).hexdigest() == sha
    for f, sha in golden["artifact_sha256"].items())
if not provenance["accepted_reference_unchanged"]:
    raise ValueError("Accepted PoC artifact changed")
shutil.copyfile(root / "benchmarks/sources/555-clock/LICENSE",
                root / "comparison/results/SOURCE-LICENSE.txt")
provenance["result_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (root / "comparison/results").iterdir()
                              if p.is_file() and p.name != "provenance.json"}
(root / "comparison/results/provenance.json").write_text(json.dumps(provenance, indent=2))
print("Pinned checkouts:", len(records), "accepted reference unchanged: yes")
