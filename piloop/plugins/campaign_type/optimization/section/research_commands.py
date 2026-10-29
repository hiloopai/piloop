from importlib.resources import files

from piloop.plugins.section.contract import Guide


class ResearchCommands(Guide):
    """The orchestrator's commands for ideas, candidates and runs."""

    TITLE = 'Ideas, candidates and runs'
    TEXT = files(__package__) / 'prompts' / 'research_commands.md'
    ORDER = 85
