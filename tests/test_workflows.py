"""A workflow that another calls (``uses: ./.github/workflows/X.yml``) gets at most the permissions that its caller
grants: one that asks for more fails to start, as a release did when CI's quarantine job asked to read issues."""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).parents[1] / '.github' / 'workflows'
LEVELS = {'none': 0, 'read': 1, 'write': 2}
DEFAULT = {'contents': 'read', 'metadata': 'read', 'packages': 'read'}
"""The token of a workflow that sets no permissions, under the repository's restricted default."""

Permissions = dict[str, str]


def permissions(value: object, inherited: Permissions) -> Permissions:
    """The permissions that a ``permissions:`` value grants, or those inherited when it is absent."""
    match value:
        case None:
            return inherited
        case 'read-all':
            return {'*': 'read'}
        case 'write-all':
            return {'*': 'write'}
        case dict():
            return {str(scope): str(level) for scope, level in value.items()}
        case _:
            raise ValueError(f'permissions: {value!r}')


def level(granted: Permissions, scope: str) -> int:
    return LEVELS[granted.get(scope, granted.get('*', 'none'))]


def missing(requested: Permissions, granted: Permissions) -> list[str]:
    """The permissions that are requested beyond what is granted, as ``scope: level``."""
    return [f'{scope}: {want}' for scope, want in requested.items() if LEVELS[want] > level(granted, scope)]


def requests(workflow: dict) -> Permissions:
    """All that the jobs of a called workflow ask for, each with its own permissions or the workflow's."""
    top = permissions(workflow.get('permissions'), DEFAULT)
    asked: Permissions = {}
    for job in workflow['jobs'].values():
        for scope, want in permissions(job.get('permissions'), top).items():
            if LEVELS[want] > LEVELS.get(asked.get(scope, 'none'), 0):
                asked[scope] = want
    return asked


def calls() -> list[tuple[str, str, Permissions, Permissions]]:
    """Each job that calls a workflow of this repository: where, which, what it grants, what the called one asks."""
    found = []
    for path in sorted(WORKFLOWS.glob('*.yml')):
        workflow = yaml.safe_load(path.read_text())
        top = permissions(workflow.get('permissions'), DEFAULT)
        for name, job in workflow['jobs'].items():
            uses = job.get('uses', '')
            if uses.startswith('./.github/workflows/'):
                called = yaml.safe_load((WORKFLOWS / Path(uses).name).read_text())
                found.append(
                    (f'{path.name} {name}', Path(uses).name, permissions(job.get('permissions'), top), requests(called))
                )
    return found


@pytest.mark.parametrize(('caller', 'called', 'granted', 'asked'), calls(), ids=lambda value: str(value))
def test_a_called_workflow_asks_for_no_more_than_its_caller_grants(
    caller: str, called: str, granted: Permissions, asked: Permissions
) -> None:
    assert not missing(asked, granted), f'{caller} calls {called} without {missing(asked, granted)}'


def test_the_check_finds_a_job_that_asks_for_more() -> None:
    ci = {'permissions': {'contents': 'read'}, 'jobs': {'quarantine': {'permissions': {'issues': 'read'}}}}
    assert missing(requests(ci), {'contents': 'read'}) == ['issues: read']
    assert missing(requests(ci), {'contents': 'read', 'issues': 'write'}) == []
    assert missing({'contents': 'write'}, permissions('read-all', DEFAULT)) == ['contents: write']
