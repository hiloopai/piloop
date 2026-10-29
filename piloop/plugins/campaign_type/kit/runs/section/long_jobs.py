from importlib.resources import files

from piloop.plugins.section.contract import Guide


class LongJobs(Guide):
    """How an implementer runs and waits for a job that outlasts one command."""

    TITLE = 'Run long jobs'
    TEXT = files(__package__) / 'prompts' / 'long_jobs.md'
    ORDER = 102
