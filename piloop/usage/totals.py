"""The tokens that agents spent, summed from ``agent_sessions.usage``."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from piloop.db.models import AgentSession
from piloop.ids import CampaignId


def campaign_tokens(s: Session, campaign_id: CampaignId, since: datetime | None = None) -> int:
    """The tokens that the agents of the campaign spent: ``SUM(json_extract(usage, '$.total'))`` over its agent sessions.

    With ``since``, only the agents requested after it.
    """
    after = () if since is None else (AgentSession.started_at > since,)
    total = s.scalar(
        select(func.sum(AgentSession.usage['total'].as_integer())).where(
            AgentSession.campaign_id == campaign_id, *after
        )
    )
    return int(total or 0)
