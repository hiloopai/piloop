"""An agent whose session has ended leaves no comment that settles its work: a TEMP trigger on ``comments``."""

from piloop.db.models import ENDED, AfterEnd, AgentSessionState
from piloop.db.triggers.ddl import Trigger

ENDED_STATES = ', '.join(f"'{state}'" for state in (AgentSessionState.ENDING, *ENDED))
"""The states of an agent session whose comments of a kind that ``AfterEnd.REFUSED`` are refused, as a SQL list."""


def no_late_comment() -> Trigger:
    """TEMP: refuse a comment of a kind that ``AfterEnd.REFUSED`` from a connection whose agent session has ended.

    Such a comment settles what the agent worked on, as a finding settles a run as finished, so one that lands after
    the daemon ended the agent would undo a stop or a cancel. The daemon sets the row ``ending`` before it kills the
    agent, and SQLite has one writer at a time: the comment commits before the end, or this trigger refuses it. A
    connection of another actor has no agent session.
    """
    return Trigger(
        'comments_no_late_comment',
        'comments',
        'BEFORE INSERT',
        when=(
            f"NEW.kind IN (SELECT id FROM comment_kinds WHERE after_end = '{AfterEnd.REFUSED}')"
            ' AND EXISTS (SELECT 1 FROM agent_sessions'
            f' WHERE id = (SELECT agent_session FROM temp.session_actor) AND state IN ({ENDED_STATES}))'
        ),
        body="SELECT RAISE(ABORT, 'the agent session of this connection has ended: its ' || NEW.kind || ' is refused')",
        temp=True,
    )


ENDED_AGENTS = (no_late_comment(),)
"""What an agent whose session has ended can no longer write."""
