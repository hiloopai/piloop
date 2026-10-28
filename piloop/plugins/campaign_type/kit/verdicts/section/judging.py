from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Judging(Guide):
    """How the daemon judges a candidate, what its verdict reasons mean, and the query of a candidate's score."""

    TITLE = 'Judge results'
    TEXT = files(__package__) / 'prompts' / 'judging.md'
    ORDER = 30
