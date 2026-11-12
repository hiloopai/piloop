"""Where a campaign's files are under the piloop home. Light: ``piloop sql``, which every agent call runs, reads it."""

from pathlib import Path

from piloop.ids import CampaignId

PROJECTS = 'projects'
"""The directory under the piloop home that holds each campaign's directory. It keeps the name of 0.1.0: a rename would
move every home's files and buy nothing."""
TEAM_DB = 'team.db'
"""The file in a campaign's directory that holds the tables its agents make: ``piloop sql`` attaches it as ``team``."""


def campaign_dir(home: Path, campaign_id: CampaignId) -> Path:
    """The directory of a campaign: its shared workspace, its team tables and the evaluations of its candidates."""
    return home / PROJECTS / str(campaign_id)


def team_db(home: Path, campaign_id: CampaignId) -> Path:
    """The tables that the agents of a campaign make (``piloop.db.sql.TEAM``). A fork starts without them."""
    return campaign_dir(home, campaign_id) / TEAM_DB
