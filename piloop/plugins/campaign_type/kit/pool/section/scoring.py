from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Scoring(Guide):
    """How the daemon scores a candidate into the pool, what makes one the best, and the query of the pool."""

    TITLE = 'Read the pool'
    TEXT = files(__package__) / 'prompts' / 'scoring.md'
    ORDER = 30
