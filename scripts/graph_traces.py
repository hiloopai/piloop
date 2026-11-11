"""Write ITF traces that cover every transition of a TLC state graph (`-dump dot,actionlabels`).

Usage: python3 -I graph_traces.py GRAPH.dot OUT_DIR [--record s] [--keep a,b,...] [--by a,b,...]

Each trace starts at an initial state and walks, by shortest paths, from one kind of transition that no trace took yet
to the nearest next one, until none is reachable from where it is. A kind is one transition of the graph, or, with
`--by`, an action between two states that agree on the variables `--by` names: what a driver can tell apart. Replays of
a graph whose states hold counters and the last action's name then take each step that the code can show once, not
each of its interleavings. The states are written as one record variable (`--record`), in the shape Quint's
`--out-itf` gives, so drivers written for Quint traces read them unchanged. `--keep` writes only the variables a
driver reads, which keeps a large graph's traces under the repo's 3 MiB file limit.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
from collections.abc import Callable
from pathlib import Path

type Itf = bool | str | list[Itf] | dict[str, Itf]
type State = dict[str, Itf]
type Edge = tuple[str, str, str]

# A label is a DOT string: `\"` inside it is an escaped quote, not its end.
NODE = re.compile(r'^(-?\d+) \[label="((?:[^"\\]|\\.)*)"(?:,|\])(.*)$')
EDGE = re.compile(r'^(-?\d+) -> (-?\d+) \[label="((?:[^"\\]|\\.)*)"')


def _parse_value(text: str) -> Itf:
    text = text.strip()
    if text in {'TRUE', 'FALSE'}:
        return text == 'TRUE'
    if text.startswith('"'):
        return text[1:-1]
    if text.startswith(('{', '<<')):
        inner = text[1:-1] if text.startswith('{') else text[2:-2]
        items = [_parse_value(item) for item in _split_top(inner)] if inner.strip() else []
        return {'#set': items} if text.startswith('{') else items
    if text.startswith('(') and ':>' in text:  # a function, `(0 :> "a" @@ 1 :> "b")`: an ITF map
        pairs = (item.partition(':>') for item in _split_top(text[1:-1], '@@'))
        return {'#map': [[_parse_value(key), _parse_value(value)] for key, _, value in pairs]}
    if text.startswith('['):  # a record or a function on strings, `[a |-> 1, b |-> 2]`: an ITF record
        pairs = (item.partition('|->') for item in _split_top(text[1:-1]))
        return {key.strip(): _parse_value(value) for key, _, value in pairs}
    return {'#bigint': str(int(text))}


def _split_top(text: str, sep: str = ',') -> list[str]:
    parts, depth, start = [], 0, 0
    for i, char in enumerate(text):
        if char in '{<[(':
            depth += 1
        elif char in '}])' or char == '>' and text[i - 1] not in '-:':  # nor does the `>` of `|->` or `:>`
            depth -= 1
        elif text.startswith(sep, i) and depth == 0 and i >= start:
            parts.append(text[start:i])
            start = i + len(sep)
    parts.append(text[start:])
    return parts


def parse_state(label: str) -> State:
    r"""One state of TLC's output, `/\ name = value` per line, with values in ITF's encoding."""
    fields: State = {}
    lines: list[str] = []
    # TLC wraps a long value onto indented lines; only a line starting with `/\` names a variable.
    for line in label.replace('\\"', '"').split('\\n'):
        if lines and not line.startswith('/\\'):
            lines[-1] += ' ' + line.strip()
        else:
            lines.append(line)
    for line in lines:
        name, _, value = line.removeprefix('/\\\\ ').removeprefix('/\\ ').partition(' = ')
        fields[name] = _parse_value(value)
    return fields


def read_graph(path: Path) -> tuple[dict[str, State], list[str], list[Edge]]:
    """The states by id, the initial state ids, and the distinct (from, to, action) transitions."""
    states: dict[str, State] = {}
    initial: list[str] = []
    edges: set[Edge] = set()
    for line in path.read_text().splitlines():
        if edge := EDGE.match(line):
            edges.add((edge[1], edge[2], edge[3]))
        elif node := NODE.match(line):
            states[node[1]] = parse_state(node[2])
            if 'style = filled' in node[3]:
                initial.append(node[1])
    return states, initial, sorted(edges)


def _cover(initial: list[str], edges: list[Edge], kind: Callable[[Edge], object]) -> list[list[Edge]]:
    """Walks from an initial state that take each kind of transition at least once.

    Each walk goes, by a shortest path, to the nearest state with a kind of transition that no walk took yet, takes it,
    and goes on from there. It ends when none is reachable, and the next walk starts again at an initial state.
    """
    out: dict[str, list[Edge]] = {}
    for edge in edges:
        out.setdefault(edge[0], []).append(edge)
    reachable, queue = set(initial), deque(initial)
    while queue:
        for edge in out.get(queue.popleft(), []):
            if edge[1] not in reachable:
                reachable.add(edge[1])
                queue.append(edge[1])
    left = {kind(edge) for edge in edges if edge[0] in reachable}
    traces = []
    while left:
        node, trace = initial[0], []
        while True:
            parent: dict[str, Edge | None] = {node: None}
            queue, found = deque([node]), None
            while queue:
                here = queue.popleft()
                if any(kind(e) in left for e in out.get(here, [])):
                    found = here
                    break
                for edge in out.get(here, []):
                    if edge[1] not in parent:
                        parent[edge[1]] = edge
                        queue.append(edge[1])
            if found is None:
                break
            path, here = [], found
            while (edge := parent[here]) is not None:
                path.append(edge)
                here = edge[0]
            step = next(e for e in out[found] if kind(e) in left)
            trace += [*path[::-1], step]
            left.difference_update(kind(e) for e in trace)
            node = step[1]
        traces.append(trace)
    return traces


def main() -> None:
    """Write the covering traces and print what they cover."""
    parser = argparse.ArgumentParser()
    parser.add_argument('graph', type=Path)
    parser.add_argument('out', type=Path)
    parser.add_argument('--record', default='s')
    parser.add_argument('--keep', type=lambda text: set(text.split(',')), default=None)
    parser.add_argument('--by', type=lambda text: set(text.split(',')), default=None)
    args = parser.parse_args()
    states, initial, edges = read_graph(args.graph)
    by = args.by

    def kind(edge: Edge) -> object:
        if by is None:
            return edge
        of = [json.dumps({k: v for k, v in states[n].items() if k in by}, sort_keys=True) for n in edge[:2]]
        return of[0], edge[2], of[1]

    traces = _cover(initial, edges, kind)
    if args.keep is not None:
        states = {n: {k: v for k, v in s.items() if k in args.keep} for n, s in states.items()}
    args.out.mkdir(parents=True, exist_ok=True)
    for seq, trace in enumerate(traces):
        nodes = [trace[0][0], *(edge[1] for edge in trace)]
        itf = {
            '#meta': {'format': 'ITF', 'source': str(args.graph), 'actions': [e[2] for e in trace]},
            'vars': [args.record],
            'states': [{'#meta': {'index': i}, args.record: states[n]} for i, n in enumerate(nodes)],
        }
        (args.out / f'trace_{seq}.itf.json').write_text(json.dumps(itf) + '\n')
    steps = sum(len(t) for t in traces)
    kinds = len({kind(e) for e in edges})
    print(f'{len(states)} states, {len(edges)} transitions of {kinds} kinds, {len(traces)} traces, {steps} steps')


if __name__ == '__main__':
    main()
