"""Invoke upstream geometry method unchanged; omit wx UI using AST extraction."""
import ast
import importlib.util
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pcbnew

source, board_path, target = map(Path, sys.argv[1:4])
scaling = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
spec = importlib.util.spec_from_file_location("round_utils", source / "round_tracks_utils.py")
utils = importlib.util.module_from_spec(spec)
spec.loader.exec_module(utils)
tree = ast.parse((source / "round_tracks_action.py").read_text())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RoundTracks")
method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "addIntermediateTracks")
namespace = dict(vars(utils), pcbnew=pcbnew, math=math, RADIUS_DEFAULT=2.0)
exec(compile(ast.Module(body=[method], type_ignores=[]), str(source / "round_tracks_action.py"), "exec"), namespace)
board = pcbnew.LoadBoard(str(board_path))
obj = SimpleNamespace(board=board, prog=SimpleNamespace(Pulse=lambda msg: None))
classes = {net.GetNetClassName() for _, net in board.GetNetsByNetcode().items()}
for name in sorted(classes):
    namespace["addIntermediateTracks"](obj, scaling=scaling, netclass=name, native=True)
pcbnew.SaveBoard(str(target), board)
print("Rounded classes:", sorted(classes))
