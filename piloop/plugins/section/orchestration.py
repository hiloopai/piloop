from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Orchestration(Guide):
    """The orchestrator's and the human's procedures: the commands that change ideas, candidates and campaigns."""

    TITLE = "The orchestrator's procedures"
    TEXT = files(__package__) / 'prompts' / 'orchestration.md'
    ORDER = 100
