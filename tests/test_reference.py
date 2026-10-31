"""Real-board acceptance: reconstructed copper, not just the search's internal state."""

from dataclasses import replace

import pytest

from auto_hand_router.cli import DEFAULT_BOARD
from auto_hand_router.model import Board
from auto_hand_router.output import board_text
from auto_hand_router.router import Options, route_board
from auto_hand_router.sexpr import parse
from auto_hand_router.validate import check


@pytest.fixture(scope="module")
def reference():
    board = Board.load(DEFAULT_BOARD)
    routes, stats = route_board(board)
    primitives = [p for r in routes for p in r.primitives]
    vias = [v for r in routes for v in r.vias]
    output = board_text(board, primitives, vias)
    return board, routes, stats, output


def test_555_export_connects_every_net_with_native_arcs(reference):
    board, routes, stats, output = reference
    result = check(board, parse(output))
    assert stats["connections_routed"] == 22
    assert result["passed"]
    assert result["unconnected_components"] == 0
    assert result["arcs"] > 0
    assert set(result["layers_used"]) == {0, 1}
    assert result["minimum_extra_clearance_mm"] >= 0
    assert all(n["components"] == 1 for n in result["connectivity"])
    assert not any(e["type"] in ("via_in_pad", "via_in_footprint") for e in result["geometry_errors"])


def test_reference_is_deterministic(reference):
    board, _, _, expected = reference
    routes, _ = route_board(board)
    actual = board_text(board, [p for r in routes for p in r.primitives], [v for r in routes for v in r.vias])
    assert actual == expected


def test_independent_validator_detects_deleted_net(reference):
    board, routes, _, _ = reference
    output = board_text(board, [p for r in routes if r.net != 6 for p in r.primitives],
                        [v for r in routes if r.net != 6 for v in r.vias])
    result = check(board, parse(output))
    assert not result["passed"]
    assert result["unconnected_components"] > 0


def test_preferred_spacing_improves_reference_sampled_gaps(reference):
    board, _, _, spacious = reference
    compact, _ = route_board(board, replace(Options(), spacing=0, relax_steps=0))
    compact_text = board_text(board, [p for r in compact for p in r.primitives],
                              [v for r in compact for v in r.vias])
    a, b = check(board, parse(spacious)), check(board, parse(compact_text))
    assert b["passed"]
    # A fixture regression, not a universal proof of spacing quality for arbitrary boards.
    assert a["spacing_samples"]["mean_gap_mm"] > b["spacing_samples"]["mean_gap_mm"] * 1.1
