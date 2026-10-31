"""Explicit opt-in to vias below through-hole module bodies; never below pads."""
from collections import Counter
from dataclasses import replace

from .sexpr import children


def via_policy(board, policy="courtyards"):
    if policy == "courtyards":
        return board, []
    if policy != "module-interiors":
        raise ValueError("Unknown via policy")
    counts = Counter(p.ref for p in board.pads if p.plated and p.drill)
    body_area = {ref:region.area for ref,region in board.via_keepouts}
    candidates = set()
    for fp in children(board.expression,"footprint"):
        ref = next((p[2] for p in children(fp,"property") if p[1]=="Reference"), "")
        # Conservative identification of large, socketed modules on these carrier
        # boards. SMD packages, small jumpers and mechanical footprints stay blocked.
        if counts[ref] >= 3 and body_area.get(ref,0) >= 50 and (ref.startswith("U") or "module" in fp[1].lower()):
            candidates.add(ref)
    return replace(board, via_keepouts=[(ref,region) for ref,region in board.via_keepouts if ref not in candidates],
                    display_regions=board.via_keepouts), sorted(candidates)
