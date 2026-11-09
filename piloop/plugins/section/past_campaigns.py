from importlib.resources import files

from piloop.plugins.section.contract import Guide


class PastCampaigns(Guide):
    """How to read what past campaigns on the repo tried, and how they worked it."""

    TITLE = 'Remember past campaigns'
    TEXT = files(__package__) / 'prompts' / 'past_campaigns.md'
    ORDER = 70
