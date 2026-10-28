from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Priorities(Guide):
    """How a comment with a priority reaches the human's inbox."""

    TITLE = 'The inbox'
    TEXT = files(__package__) / 'prompts' / 'priorities.md'
    ORDER = 50
