from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Comments(Guide):
    """How agents talk to each other in comments, and what they never change."""

    TITLE = 'Talk'
    TEXT = files(__package__) / 'prompts' / 'comments.md'
    ORDER = 40
