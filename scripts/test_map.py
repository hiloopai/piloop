"""The test map: which tests ran, and the lines that each one alone covers, from a run with test contexts.

    uv run -m scripts.test_map build .coverage/.coverage --map test-map.json.gz

CI builds it nightly on main (``--cov-context=test``, which needs coverage's ctrace core) and caches it. The files where
a test alone covers lines are what a quarantine of that test leaves out of coverage, and a test that covers no line
alone adds no coverage.
"""

import argparse
import gzip
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CoverageMap:
    """The tests of a run, and for each one the files where it alone covers lines, with how many."""

    commit: str
    tests: tuple[str, ...]
    unique: dict[str, dict[str, int]]

    def save(self, path: Path) -> None:
        payload = {'commit': self.commit, 'tests': self.tests, 'unique': self.unique}
        path.write_bytes(gzip.compress(json.dumps(payload).encode()))

    @classmethod
    def load(cls, path: Path) -> CoverageMap:
        data = json.loads(gzip.decompress(path.read_bytes()))
        return cls(data['commit'], tuple(data['tests']), data['unique'])


def build(coverage_file: Path, root: Path, commit: str) -> CoverageMap:
    """The map of a coverage data file with test contexts; ``root`` makes its paths relative."""
    from coverage import CoverageData

    data = CoverageData(str(coverage_file))
    data.read()
    unique: dict[str, dict[str, int]] = {}
    for measured in data.measured_files():
        relative = str(Path(measured).resolve().relative_to(root))
        for contexts in data.contexts_by_lineno(measured).values():
            tests = {context.split('|')[0] for context in contexts if context}
            if len(tests) == 1:
                files = unique.setdefault(tests.pop(), {})
                files[relative] = files.get(relative, 0) + 1
    tests = sorted({context.split('|')[0] for context in data.measured_contexts() if context})
    return CoverageMap(commit, tuple(tests), unique)


def main() -> None:
    """The command line."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    build_parser = commands.add_parser('build', help='write the map of a coverage data file with test contexts')
    build_parser.add_argument('coverage', type=Path)
    build_parser.add_argument('--map', type=Path, required=True)
    args = parser.parse_args()
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    build(args.coverage, Path.cwd().resolve(), commit).save(args.map)


if __name__ == '__main__':
    main()
