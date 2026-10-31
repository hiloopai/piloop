"""Tell a campaign's running workers where the head moved, for the roles with ``follow_head = "message"``.

When another run's version becomes the head, each running worker of such a role gets one message that names its
revision, to merge. The head is pinned in the repo of record (``Repository.keep``), which keeps it reachable. A worktree
shares its repo's objects, so every worker whose checkout is a worktree of the repo of record (the daemon's own) can
merge it at once. A worker on another machine checks out of its own replica, which lacks the commit: it gets the
message, but nothing brings the commit to it before its run ends.
"""

from contextlib import closing

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.agents.results import queue_prompt
from piloop.campaign.campaigns import repository
from piloop.db.models import Campaign, Comment
from piloop.ids import CommentId, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.runs.ids import RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, latest_agent_session
from piloop.plugins.campaign_type.kit.verdicts.judge import head
from piloop.plugins.role.contract import FollowHead
from piloop.plugins.vcs.contract import Pin

HEAD = 'head'
"""The name of the head's pin in the campaign: it keeps the head's version reachable in the repo of record."""
MOVED = '[piloop] The head moved to {revision} (candidate#{candidate}). Merge {revision} into your work to build on it.'
"""The message to a running worker when the head moved."""


def follow_head(s: Session, campaign: Campaign) -> list[CommentId]:
    """Pin the head, and message each running worker of a following role that started before it, once per head.

    A worker that started from the head, and the run whose version it is, get nothing. The caller commits.
    """
    roles = [
        name for name, role in campaign.config.roles.doing_runs().items() if role.follow_head is FollowHead.MESSAGE
    ]
    top = head(s, campaign) if roles else None
    if top is None:
        return []
    with closing(repository(campaign)) as repo:
        repo.keep(top.revision, Pin(campaign.name, HEAD))
    text = MOVED.format(revision=top.revision, candidate=top.id)
    told = []
    running = select(Run).where(Run.campaign_id == campaign.id, Run.status == RunStatus.RUNNING, Run.role.in_(roles))
    for run in s.scalars(running):
        agent = latest_agent_session(s, run.id)
        if run.id == top.run_id or run.base_candidate_id == top.id or agent is None:
            continue
        target = TargetRef(TargetKind.AGENT_SESSION, agent.id)
        sent = exists().where(Comment.target == target, Comment.kind == CommentKind.MESSAGE, Comment.body == text)
        if not s.scalar(select(sent)) and (comment := queue_prompt(s, agent.id, text, System.DAEMON)) is not None:
            told.append(comment)  # None: its worker is not running yet, and starts from what it gets then
    return told
