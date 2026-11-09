"""``campaigns.repo``: a repository is named by its remote, the same for every clone of it."""

from pathlib import Path

import pytest
from git import Repo

from piloop.plugins.vcs.git.commands import identity


@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        ('git@github.com:example/project.git', 'github.com/example/project'),
        ('https://github.com/example/project', 'github.com/example/project'),
        ('https://user@github.com/example/project.git/', 'github.com/example/project'),
        ('ssh://git@example.com:2222/team/repo.git', 'example.com:2222/team/repo'),
        ('ssh://git@github.com:22/example/project.git', 'github.com/example/project'),
        ('HTTPS://GitHub.com/example/project', 'github.com/example/project'),
        ('https://github.com:443/example/project', 'github.com/example/project'),
        ('git+ssh://git@github.com/example/project', 'github.com/example/project'),
        ('/srv/git/project.git', '/srv/git/project'),
    ],
)
def test_a_repo_is_identified_by_its_remote_whatever_the_clone(tmp_path: Path, url: str, expected: str) -> None:
    repo = Repo.init(tmp_path / 'clone')
    repo.create_remote('upstream', 'https://example.com/other/repo')
    repo.create_remote('origin', url)
    assert identity(tmp_path / 'clone') == expected, 'origin wins over the other remotes'


def test_without_origin_the_first_remote_names_the_repo_and_without_any_the_path(tmp_path: Path) -> None:
    repo = Repo.init(tmp_path / 'clone')
    assert identity(tmp_path / 'clone') == str(tmp_path / 'clone')
    repo.create_remote('zeta', 'https://example.com/z/repo')
    repo.create_remote('fork', 'git@github.com:example/project.git')
    assert identity(tmp_path / 'clone') == 'github.com/example/project'
    assert identity(tmp_path / 'nowhere') == str(tmp_path / 'nowhere')
