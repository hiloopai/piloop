from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Database(Guide):
    """How to run ``piloop sql``, and how to quote an agent's own text. Every built-in role shows it."""

    TITLE = 'Working with the piloop database'
    TEXT = files(__package__) / 'prompts' / 'database.md'
    ORDER = 10
