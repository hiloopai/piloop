"""Keep the suite healthy without a person: quarantine a flaky test, and propose to delete slow tests that add nothing.

    uv run -m scripts.test_health issue NODE_ID --run URL --branch NAME JUNIT...
    uv run -m scripts.test_health quarantine NODE_ID --issue N --title TITLE --map test-map.json.gz
    uv run -m scripts.test_health report --junit DIR --map test-map.json.gz --slow 10 --most 3

``issue`` prints the body of a flaky test's issue, with its failure from the JUnit files. ``quarantine`` marks a test ``@pytest.mark.quarantine(issue=N)`` and adds the issue's table to ``tests/quarantine.toml``,
with the files of the lines that only this test covers as ``unmeasured`` (the test map's ``unique``).

``report`` reads the JUnit files of main's recent CI runs, and prints each slow test's runs, failures and median time as
Markdown. Of the tests whose median is at least ``--slow`` seconds and that cover no line that another test does not,
the ``--most`` slowest are deleted from their modules, when every case of a function is; a slow one that covers a line alone is listed for a
rewrite. A person reviews the deletions: a pull request that deletes tests waits for a review.
"""

import argparse
import ast
import statistics
import tomllib
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from scripts.test_map import CoverageMap

QUARANTINE = Path('tests/quarantine.toml')


def function_of(module: ast.Module, path: list[str]) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The test function or method at ``Class::name`` or ``name`` of a module."""
    body: list[ast.stmt] = module.body
    for i, name in enumerate(path):
        node = next(
            n for n in body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) and n.name == name
        )
        match node, i == len(path) - 1:
            case ast.FunctionDef() | ast.AsyncFunctionDef(), True:
                return node
            case ast.ClassDef(), False:
                body = node.body
            case _:
                raise ValueError(f'no test {"::".join(path)}')
    raise ValueError('an empty node id')


def issue(nodeid: str, run: str, branch: str, junit: list[Path]) -> str:
    """The body of a flaky test's issue: where it failed, and how."""
    file, names = split(nodeid)
    failures = [
        (bad.text or bad.get('message', ''))[-3000:]
        for path in junit
        for case in ET.parse(path).getroot().iter('testcase')
        if nodeid_of(case.get('classname', ''), case.get('name', '')) == nodeid
        for bad in (case.find('failure'), case.find('error'))
        if bad is not None
    ]
    return '\n'.join(
        [
            f'**What:** `{nodeid}` failed in [this CI run]({run}) on `{branch}`, then passed on its rerun.',
            '',
            'It is quarantined until this issue is fixed. The fix deletes its table in tests/quarantine.toml and its mark.',
            '',
            '**The failure:**',
            '```',
            *failures,
            '```',
            '',
        ]
    )


def split(nodeid: str) -> tuple[Path, list[str]]:
    """A node id's file, and the names of its function, without parameters."""
    file, *names = nodeid.split('[')[0].split('::')
    return Path(file), names


def quarantine(nodeid: str, issue: int, title: str, coverage_map: CoverageMap) -> None:
    """Mark a test quarantined under an issue, and add the issue's table."""
    file, names = split(nodeid)
    lines = file.read_text().splitlines(keepends=True)
    module = ast.parse(''.join(lines))
    node = function_of(module, names)
    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    lines.insert(first - 1, f'{" " * node.col_offset}@pytest.mark.quarantine(issue={issue})\n')
    if not any(isinstance(n, ast.Import) and any(a.name == 'pytest' for a in n.names) for n in module.body):
        lines.insert(0, 'import pytest\n')
    file.write_text(''.join(lines))
    cases = [test for test in coverage_map.tests if split(test) == (file, names)]
    unmeasured = sorted({path for case in cases for path in coverage_map.unique.get(case, {})})
    table = {'title': title, **({'unmeasured': unmeasured} if unmeasured else {})}
    tomllib.loads(QUARANTINE.read_text())  # it parses before it grows
    with QUARANTINE.open('a') as out:
        out.write('\n' + tomli_w.dumps({str(issue): table}))


@dataclass
class History:
    """A test's runs on main: the time of each, and how many failed."""

    times: list[float]
    failures: int = 0


def histories(junit: Path) -> dict[str, History]:
    """Each test's runs in a directory of JUnit files, by node id."""
    found: dict[str, History] = defaultdict(lambda: History([]))
    for report in junit.rglob('*.xml'):
        for case in ET.parse(report).getroot().iter('testcase'):
            if case.find('skipped') is not None:
                continue
            history = found[nodeid_of(case.get('classname', ''), case.get('name', ''))]
            history.times.append(float(case.get('time', '0')))
            history.failures += case.find('failure') is not None or case.find('error') is not None
    return found


def nodeid_of(classname: str, name: str) -> str:
    """The node id of a JUnit test case: its ``classname`` is the module's dotted path, then its classes."""
    parts = classname.split('.')
    for cut in range(len(parts), 0, -1):
        path = Path(*parts[:cut]).with_suffix('.py')
        if path.is_file():
            return '::'.join([path.as_posix(), *parts[cut:], name])
    return f'{classname}::{name}'


def report(junit: Path, coverage_map: CoverageMap, slow: float, most: int) -> str:
    """The slow tests as Markdown; the ``most`` slowest that add no coverage are deleted from their modules."""
    runs = histories(junit)
    medians = {test: statistics.median(h.times) for test, h in runs.items() if h.times}
    known = set(coverage_map.tests)
    slow_tests = {test for test, median in medians.items() if median >= slow and test in known}
    alone = {test: sum(coverage_map.unique.get(test, {}).values()) for test in slow_tests}
    by_function: dict[tuple[Path, tuple[str, ...]], list[str]] = defaultdict(list)
    for test in coverage_map.tests:
        file, names = split(test)
        by_function[file, tuple(names)].append(test)
    candidates = [
        key for key, cases in by_function.items() if all(case in slow_tests and alone[case] == 0 for case in cases)
    ]
    slowest = sorted(candidates, key=lambda key: -sum(medians[case] for case in by_function[key]))
    deleted = slowest[:most]
    for file in {file for file, _ in deleted}:
        delete(file, [list(names) for f, names in deleted if f == file])
    lines = [
        f"Main's CI runs over the last days: {len(runs)} tests, {len(slow_tests)} with a median of {slow:g} s or more.",
        '',
        '| Test | Runs | Failures | Median | Lines only it covers | Proposal |',
        '| --- | ---: | ---: | ---: | ---: | --- |',
    ]
    for test in sorted(slow_tests, key=lambda t: (-medians[t], t)):
        file, names = split(test)
        key = (file, tuple(names))
        proposal = (
            'delete'
            if key in deleted
            else 'next week'
            if key in candidates
            else 'rewrite faster'
            if alone[test]
            else 'keep (a case covers lines)'
        )
        lines.append(
            f'| `{test}` | {len(runs[test].times)} | {runs[test].failures} | {medians[test]:.1f} s | {alone[test]} | {proposal} |'
        )
    return '\n'.join(lines) + '\n'


def delete(file: Path, functions: list[list[str]]) -> None:
    """Delete test functions, with their decorators, from a module."""
    lines = file.read_text().splitlines(keepends=True)
    module = ast.parse(''.join(lines))
    spans = []
    for names in functions:
        node = function_of(module, names)
        spans.append((min([node.lineno, *(d.lineno for d in node.decorator_list)]), node.end_lineno or node.lineno))
    for first, last in sorted(spans, reverse=True):
        del lines[first - 1 : last]
    file.write_text(''.join(lines))


def main() -> None:
    """The command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    issue_parser = commands.add_parser('issue', help="print the body of a flaky test's issue")
    issue_parser.add_argument('nodeid')
    issue_parser.add_argument('--run', required=True)
    issue_parser.add_argument('--branch', required=True)
    issue_parser.add_argument('junit', type=Path, nargs='*')
    quarantine_parser = commands.add_parser('quarantine', help='quarantine a flaky test under its issue')
    quarantine_parser.add_argument('nodeid')
    quarantine_parser.add_argument('--issue', type=int, required=True)
    quarantine_parser.add_argument('--title', required=True)
    quarantine_parser.add_argument('--map', type=Path, required=True)
    report_parser = commands.add_parser('report', help='print the slow tests, and delete the ones that add nothing')
    report_parser.add_argument('--junit', type=Path, required=True)
    report_parser.add_argument('--map', type=Path, required=True)
    report_parser.add_argument('--slow', type=float, required=True, help='the median seconds of a slow test')
    report_parser.add_argument('--most', type=int, required=True, help='how many tests one report deletes at most')
    args = parser.parse_args()
    match args.command:
        case 'issue':
            print(issue(args.nodeid, args.run, args.branch, args.junit), end='')
        case 'quarantine':
            quarantine(args.nodeid, args.issue, args.title, CoverageMap.load(args.map))
        case _:
            print(report(args.junit, CoverageMap.load(args.map), args.slow, args.most), end='')


if __name__ == '__main__':
    main()
