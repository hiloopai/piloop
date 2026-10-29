from importlib.resources import files

from piloop.plugins.section.contract import Guide


class SharedWorkspace(Guide):
    """What an implementer may write in the shared workspace, and where."""

    TITLE = 'The workspace'
    TEXT = files(__package__) / 'prompts' / 'workspace.md'
    ORDER = 104
