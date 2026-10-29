import re
from pathlib import Path

from tests.quarantine import ISSUES

TESTS = Path(__file__).parent
MARK = re.compile(r'pytest\.mark\.quarantine\b(?:\(issue=(\d+)\))?')


def test_each_quarantined_test_names_an_open_issue_and_each_open_issue_quarantines_a_test() -> None:
    """A fix deletes its issue's table, which fails every mark that still names it: the test comes back."""
    marks = [m for path in TESTS.rglob('*.py') if path != Path(__file__) for m in MARK.finditer(path.read_text())]
    assert all(m[1] for m in marks), 'a quarantine mark names its issue: pytest.mark.quarantine(issue=N)'
    assert {int(m[1]) for m in marks} == ISSUES.keys(), 'each mark names an issue of tests/quarantine.toml, and back'
    unmeasured = [Path(p) for issue in ISSUES.values() for p in issue.unmeasured]
    assert all((TESTS.parent / p).is_file() for p in unmeasured), 'an unmeasured file is a file of the repository'
