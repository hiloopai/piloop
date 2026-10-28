from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Search(Guide):
    """How to search the ideas and the comments in full text."""

    TITLE = 'Search'
    TEXT = files(__package__) / 'prompts' / 'search.md'
    ORDER = 60
