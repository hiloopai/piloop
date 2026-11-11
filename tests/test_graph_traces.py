"""``scripts/graph_traces.py``: walks that cover every kind of transition of a state graph."""

from scripts.graph_traces import _cover


def test_a_kind_that_only_a_later_initial_state_reaches_is_covered_from_there() -> None:
    """Two initial states, as a model whose clocks start at several offsets has: each walk starts where it can go on."""
    edges = [('a', 'b', 'Step'), ('c', 'd', 'Other')]
    traces = _cover(['a', 'c'], edges, lambda edge: edge[2])
    assert traces == [[('a', 'b', 'Step')], [('c', 'd', 'Other')]]
