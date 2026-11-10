"""Write ITF traces that cover every transition of a TLC state graph (`-dump dot,actionlabels`).

Usage: python3 -I graph_traces.py GRAPH.dot OUT_DIR [--record s] [--keep a,b,...]

Each trace starts at an initial state and takes the shortest path to a transition no trace has
covered yet, then keeps walking along uncovered transitions until none leaves its state. The
states are written as one record variable (`--record`), in the shape Quint's `--out-itf` gives,
so drivers written for Quint traces read them unchanged. `--keep` writes only the variables a
driver reads, which keeps a large graph's traces under the repo's 3 MiB file limit.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import deque
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


def _cover(initial: list[str], edges: list[Edge]) -> list[list[Edge]]:
    out: dict[str, list[Edge]] = {}
    for edge in edges:
        out.setdefault(edge[0], []).append(edge)
    parent: dict[str, Edge | None] = dict.fromkeys(initial)
    queue = deque(initial)
    while queue:
        node = queue.popleft()
        for edge in out.get(node, []):
            if edge[1] not in parent:
                parent[edge[1]] = edge
                queue.append(edge[1])

    def path_to(node: str) -> list[Edge]:
        steps = []
        while (edge := parent[node]) is not None:
            steps.append(edge)
            node = edge[0]
        return steps[::-1]

    covered: set[Edge] = set()
    traces = []
    for edge in edges:
        if edge in covered or edge[0] not in parent:
            continue
        trace = [*path_to(edge[0]), edge]
        while step := next((e for e in out.get(trace[-1][1], []) if e not in covered and e not in trace), None):
            trace.append(step)
        covered.update(trace)
        traces.append(trace)
    return traces


def main() -> None:
    """Write the covering traces and print what they cover."""
    parser = argparse.ArgumentParser()
    parser.add_argument('graph', type=Path)
    parser.add_argument('out', type=Path)
    parser.add_argument('--record', default='s')
    parser.add_argument('--keep', type=lambda text: set(text.split(',')), default=None)
    args = parser.parse_args()
    states, initial, edges = read_graph(args.graph)
    if args.keep is not None:
        states = {n: {k: v for k, v in s.items() if k in args.keep} for n, s in states.items()}
    traces = _cover(initial, edges)
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
    print(f'{len(states)} states, {len(edges)} transitions, {len(traces)} traces, {steps} steps')


if __name__ == '__main__':
    main()
