from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Concepts(Guide):
    """Campaigns, ideas, runs, candidates, measurements, the pool, its best and the tracks."""

    TITLE = 'The model'
    TEXT = files(__package__) / 'prompts' / 'concepts.md'
    ORDER = 20
