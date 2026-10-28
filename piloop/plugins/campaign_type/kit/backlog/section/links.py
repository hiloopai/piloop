from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Links(Guide):
    """What ``after`` and ``supersedes`` mean, and how to link a new idea."""

    TITLE = 'Link ideas'
    TEXT = files(__package__) / 'prompts' / 'links.md'
    ORDER = 80
