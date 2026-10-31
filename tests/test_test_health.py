"""scripts/test_health.py: a flaky test quarantines itself, and a slow test that covers nothing alone is deleted."""

from pathlib import Path

import pytest

from scripts import test_health
from scripts.test_map import CoverageMap

MODULE = """import pytest


def test_slow_and_redundant() -> None:
    assert True


@pytest.mark.parametrize('x', [1, 2])
def test_slow_but_alone(x: int) -> None:
    assert x


class TestGroup:
    def test_fast(self) -> None:
        assert True
"""
MARK = '@' + '.'.join(
    ['pytest', 'mark', 'quarantine']
)  # split, so that tests/test_quarantine.py does not take it for a mark
TESTS = (
    'tests/test_m.py::test_slow_and_redundant',
    'tests/test_m.py::test_slow_but_alone[1]',
    'tests/test_m.py::test_slow_but_alone[2]',
    'tests/test_m.py::TestGroup::test_fast',
)
COVERAGE_MAP = CoverageMap('c0ffee', TESTS, {TESTS[1]: {'piloop/a.py': 3}, TESTS[3]: {'piloop/b.py': 1}})
JUNIT = """<testsuites><testsuite>
<testcase classname="tests.test_m" name="test_slow_and_redundant" time="{slow}"/>
<testcase classname="tests.test_m" name="test_slow_but_alone[1]" time="{slow}"><failure/></testcase>
<testcase classname="tests.test_m" name="test_slow_but_alone[2]" time="0.1"/>
<testcase classname="tests.test_m.TestGroup" name="test_fast" time="0.1"/>
</testsuite></testsuites>"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_m.py').write_text(MODULE)
    (tmp_path / 'tests/quarantine.toml').write_text('# The quarantined tests.\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_quarantine_marks_the_test_and_names_the_files_it_alone_covers(repo: Path) -> None:
    test_health.quarantine('tests/test_m.py::TestGroup::test_fast', 7, 'test: it is flaky', COVERAGE_MAP)
    test_health.quarantine('tests/test_m.py::test_slow_but_alone[2]', 8, 'test: so is this', COVERAGE_MAP)
    assert (repo / 'tests/test_m.py').read_text() == MODULE.replace(
        "@pytest.mark.parametrize('x'", f"{MARK}(issue=8)\n@pytest.mark.parametrize('x'"
    ).replace('    def test_fast', f'    {MARK}(issue=7)\n    def test_fast')
    assert (repo / 'tests/quarantine.toml').read_text() == (
        '# The quarantined tests.\n\n[7]\ntitle = "test: it is flaky"\nunmeasured = [\n    "piloop/b.py",\n]\n'
        '\n[8]\ntitle = "test: so is this"\nunmeasured = [\n    "piloop/a.py",\n]\n'
    )


def test_a_slow_test_that_covers_nothing_alone_is_deleted_and_one_that_does_is_listed(repo: Path) -> None:
    for run, slow in enumerate(['12.0', '14.0', '30.0']):
        (repo / f'junit/{run}').mkdir(parents=True)
        (repo / f'junit/{run}/part.xml').write_text(JUNIT.format(slow=slow))
    report = test_health.report(repo / 'junit', COVERAGE_MAP, slow=10, most=1)
    assert report.splitlines()[4:] == [
        '| `tests/test_m.py::test_slow_and_redundant` | 3 | 0 | 14.0 s | 0 | delete |',
        '| `tests/test_m.py::test_slow_but_alone[1]` | 3 | 3 | 14.0 s | 3 | rewrite faster |',
    ]
    assert 'def test_slow_and_redundant' not in (repo / 'tests/test_m.py').read_text()
    assert 'def test_slow_but_alone' in (repo / 'tests/test_m.py').read_text()


def test_a_flaky_issue_says_where_and_how_the_test_failed(repo: Path) -> None:
    (repo / 'junit.xml').write_text(
        JUNIT.format(slow='1').replace('<failure/>', '<failure>AssertionError: 1 != 2</failure>')
    )
    body = test_health.issue('tests/test_m.py::test_slow_but_alone[1]', 'https://ci/1', 'feat/x', [repo / 'junit.xml'])
    assert body.splitlines() == [
        '**What:** `tests/test_m.py::test_slow_but_alone[1]` failed in [this CI run](https://ci/1) on `feat/x`, then '
        'passed on its rerun.',
        '',
        'It is quarantined until this issue is fixed. The fix deletes its table in tests/quarantine.toml and its mark.',
        '',
        '**The failure:**',
        '```',
        'AssertionError: 1 != 2',
        '```',
    ]
