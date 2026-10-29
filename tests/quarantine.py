"""The open issues of the quarantined tests (``tests/quarantine.toml``), and a coverage plugin that leaves the files
that only their tests cover out of the required run's coverage (``[tool.coverage.run] plugins``)."""

import tomllib
from pathlib import Path

from coverage import CoveragePlugin
from coverage.plugin_support import Plugins
from coverage.types import TConfigurable
from pydantic import BaseModel, TypeAdapter


class Issue(BaseModel, extra='forbid'):
    title: str
    unmeasured: tuple[str, ...] = ()


ISSUES = TypeAdapter(dict[int, Issue]).validate_python(
    tomllib.loads((Path(__file__).parent / 'quarantine.toml').read_text())
)


class Unmeasured(CoveragePlugin):
    def configure(self, config: TConfigurable) -> None:
        omit = config.get_option('run:omit')
        assert isinstance(omit, list)
        config.set_option('run:omit', [*omit, *(path for issue in ISSUES.values() for path in issue.unmeasured)])


def coverage_init(reg: Plugins, options: dict[str, str]) -> None:
    reg.add_configurer(Unmeasured())
