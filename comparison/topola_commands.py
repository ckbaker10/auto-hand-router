"""Generate the documented serialized command history for a two-layer run."""
import json
from pathlib import Path
import sys

d = json.loads(Path(sys.argv[1]).read_text())
selection = [dict(pin=p["id"].replace(".", "-"), layer="F.Cu")
             for p in d["pads"] if p["plated"] and p["net"]]
planar = dict(principal_layer=0, presort_by="RatlineIntersectionCountAndLength",
              permutate=True, router=dict(routed_band_width=508.0,
              wrap_around_bands=True, squeeze_through_under_bends=False),
              timeout=dict(initial=10.0, progress_bonus=0.005))
options = dict(anterouter=dict(fanout_clearance=254.0), planar=planar,
               timeout=dict(initial=60.0, progress_bonus=0.005))
Path(sys.argv[2]).write_text(json.dumps(dict(done=[dict(
    MultilayerAutoroute=[selection, options])], undone=[]), indent=2))
