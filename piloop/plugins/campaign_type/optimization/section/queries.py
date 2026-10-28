from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Queries(Guide):
    """Queries of the running runs, the queue and its prerequisites, and what was said about an idea."""

    TITLE = 'Useful queries'
    TEXT = files(__package__) / 'prompts' / 'queries.md'
    ORDER = 95
