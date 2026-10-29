"""The agent session that made a candidate, through its run, and the tokens that it spent."""

from sqlalchemy.orm import Session

from piloop.db.models import AgentSession
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.runs.tables import latest_agent_session


def session_agent_session(s: Session, candidate: Candidate) -> AgentSession | None:
    """Return the implementer whose session made ``candidate`` (the session whose subject is its run). None for the baseline."""
    return latest_agent_session(s, candidate.run_id) if candidate.run_id is not None else None


def session_tokens(s: Session, candidate: Candidate) -> int | None:
    """Return the tokens that the session that made ``candidate`` spent (``agent_sessions.usage`` total). None when unknown."""
    agent_session = session_agent_session(s, candidate)
    return agent_session.usage.total if agent_session is not None and agent_session.usage else None
