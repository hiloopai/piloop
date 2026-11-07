from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Database(Guide):
    """How to run ``piloop sql``, and how to quote an agent's own text. A role that does no runs shows it by default."""

    TITLE = 'Working with the piloop database'
    TEXT = files(__package__) / 'prompts' / 'database.md'
    ORDER = 10
