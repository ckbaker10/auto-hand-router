"""Add via-only body regions to KiCad's DSN without blocking tracks."""
import json
from pathlib import Path
import sys

data = json.loads(Path(sys.argv[1]).read_text())
source = Path(sys.argv[2]).read_text()
strict = "--strict" in sys.argv[4:]
regions = []
for index, area in enumerate(data["keepouts"]):
    # DSN units are micrometres and Y has the opposite orientation to KiCad.
    outline = area["clearance_outline"] if strict else area["outline"]
    coords = " ".join(f"{x*1000:.3f} {-y*1000:.3f}" for x, y in outline)
    regions.append(f'    (via_keepout "body_{index}" (polygon signal 0 {coords}))')
marker = "  (structure\n"
if source.count(marker) != 1 or "(unit um)" not in source:
    raise ValueError("Unexpected DSN units/structure")
if strict:
    # Conservative common via size: source netclasses allow 0.889/0.381 and
    # 1.016/0.508 mm. Keep the larger existing padstack as the only choice.
    old = '(via "Via[0-1]_889:381_um" "Via[0-1]_1016:508_um")'
    if source.count(old) != 1:
        raise ValueError("Unexpected via types for this 555 fixture")
    source = source.replace(old, '(via "Via[0-1]_1016:508_um")')
    source = source.replace('(use_via "Via[0-1]_889:381_um")',
                            '(use_via "Via[0-1]_1016:508_um")')
Path(sys.argv[3]).write_text(source.replace(marker, marker + "\n".join(regions) + "\n", 1))
print("Via-only body regions:", len(regions))
