"""scripts/test_map.py: the lines that each test alone covers, from coverage data with test contexts."""

from pathlib import Path

from coverage import CoverageData

from scripts.test_map import CoverageMap, build


def test_a_line_that_one_test_covers_counts_for_it_and_one_that_two_cover_for_neither(tmp_path: Path) -> None:
    module = tmp_path / 'piloop' / 'a.py'
    data = CoverageData(str(tmp_path / '.coverage'))
    for context, lines in [
        ('', [1]),
        ('tests/test_a.py::test_one|run', [1, 2, 3]),
        ('tests/test_a.py::test_two|setup', [3]),
    ]:
        data.set_context(context)
        data.add_lines({str(module): lines})
    data.write()
    built = build(tmp_path / '.coverage', tmp_path, 'c0ffee')
    assert built == CoverageMap(
        'c0ffee',
        ('tests/test_a.py::test_one', 'tests/test_a.py::test_two'),
        {'tests/test_a.py::test_one': {'piloop/a.py': 2}},
    )
    built.save(tmp_path / 'map.json.gz')
    assert CoverageMap.load(tmp_path / 'map.json.gz') == built
