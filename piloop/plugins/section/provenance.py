from importlib.resources import files

from piloop.plugins.section.contract import Guide


class Provenance(Guide):
    """Who changed a row, and when: ``updated_by`` and the change log in ``events``."""

    TITLE = 'Provenance is automatic'
    TEXT = files(__package__) / 'prompts' / 'provenance.md'
    ORDER = 90
