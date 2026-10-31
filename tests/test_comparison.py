"""Check exchange fidelity and prevent spacing scores depending on segmentation."""
from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest

from auto_hand_router.model import Board, Pad, Primitive, Rules
from shapely.geometry import Point, box

spec = importlib.util.spec_from_file_location(
    "comparison_evaluate", Path(__file__).parents[1] / "comparison/evaluate.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def fixture_board():
    return Board(Path("input"), Path("project"), "", [],
                 [Pad("X", "1", 2, (5, 8), (0,), Point(5, 8).buffer(0.5))],
                 box(0, 0, 10, 10), {1: "A", 2: "B"},
                 {1: Rules(), 2: Rules()}, [], [])


def test_exchange_retains_arcs_widths_netnames_and_via_geometry():
    board = fixture_board()
    data = dict(tracks=[dict(net="A", layer=1, width=0.508, start=[1, 2],
                             mid=[2, 1], end=[3, 2])],
                vias=[dict(net="A", center=[3, 2], diameter=1.016, drill=0.508)])
    tracks, vias = evaluation.copper(data, board)
    assert tracks[0] == Primitive(1, 1, 0.508, (1, 2), (3, 2), (2, 1))
    assert vias[0].diameter == 1.016
    assert vias[0].drill == 0.508
    assert vias[0].net == 1
    with pytest.raises(ValueError, match="layer"):
        evaluation.copper(dict(tracks=[dict(data["tracks"][0], layer=2)], vias=[]), board)


def test_spacing_is_invariant_to_exporter_splitting_a_straight_track():
    board = fixture_board()
    track = Primitive(1, 0, 0.381, (1, 2), (9, 2))
    fragments = [replace(track, start=(1+i, 2), end=(2+i, 2)) for i in range(8)]
    whole = evaluation.uniform_spacing(board, [track], [])
    split = evaluation.uniform_spacing(board, fragments, [])
    assert whole["mean_gap_mm"] == pytest.approx(split["mean_gap_mm"], abs=1e-12)
    assert whole["sample_count"] == split["sample_count"]
