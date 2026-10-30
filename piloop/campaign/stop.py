"""Stop conditions: decide when a campaign's stop rule, or a request, ends the campaign.

The scheduler checks every active campaign once per pass. The reason goes into ``campaigns.stopped_reason`` and into
the trigger of the closing watchdog. A stop or a cancel that someone asks for is an event that the daemon acts on and
answers (``piloop.db.requests``).
"""

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import ClassVar, Literal, assert_never

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from piloop.actor import Actor, System
from piloop.campaign import escalations
from piloop.config import CampaignConfig
from piloop.db.events import emit, has, in_order, latest
from piloop.db.models import (
    LIVE,
    AgentSession,
    AgentSessionState,
    Campaign,
    Event,
    EventModel,
    ReadBy,
    count,
    utcnow,
)
from piloop.db.requests import answer, pending
from piloop.db.triggers import DAEMON_ACTOR
from piloop.db.triggers.changelog import CampaignPausedChanged, CampaignStatusChanged
from piloop.ids import CampaignStatus, Ending, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.runs.ids import RUN, RunId
from piloop.plugins.campaign_type.kit.runs.tables import Run, campaign_runs, run_of, working_on
from piloop.plugins.escalation.contract import Choice, EscalationView
from piloop.plugins.fact_source.facts import HOURS, Fact, Facts, FactValue
from piloop.plugins.stop_rule.contract import STOP_RULES, Escalate, Go, Hold, Stop, StopCase, WhileHeld


class StopRequested(EventModel):
    """Someone asked to stop a campaign (``piloop campaign stop``).

    On its next pass, the daemon stops an active campaign as for a ``[stop]`` condition (``check``). It ends the
    campaign's implementers as ``ending`` says, also when the campaign was stopped already. An ask
    (``piloop.db.requests``).
    """

    kind: Literal['campaign.stop_requested'] = 'campaign.stop_requested'
    reason: str
    """Why, or empty."""
    ending: Ending


class CampaignReopened(EventModel):
    """Someone set a stopped campaign active again (``piloop campaign reopen``).

    The runs, hours and tokens that the stop rule limits count from the latest reopen (``reopened_at``).
    """

    kind: Literal['campaign.reopened'] = 'campaign.reopened'
    READ_BY: ClassVar[ReadBy] = ReadBy.READERS  # its ``campaign.status`` wakes the consumers
    reason: str


def reopened_at(s: Session, campaign: Campaign) -> datetime | None:
    """When the campaign was last reopened (``CampaignReopened``), or None if it never was."""
    on_campaign = Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id)
    return s.scalar(select(func.max(Event.at)).where(has(CampaignReopened), on_campaign))


class CancelRequested(EventModel):
    """Someone asked to cancel a run (``piloop run cancel``) or an agent (``piloop agent cancel``).

    The daemon closes a requested row with its queued run. It ends a running agent with everything it started, as
    ``ending`` says. A starting agent waits until it runs, and a run whose agent then fails to start is not launched
    again. The run ends ``cancelled`` unless its worker ended it first (``piloop run end``), and its commits are scored. An ask
    (``piloop.db.requests``).
    """

    kind: Literal['run.cancel_requested', 'agent_session.cancel_requested']
    ending: Ending


CANCELLABLE = (AgentSessionState.REQUESTED, *LIVE)
"""The states of an agent that a cancel acts on: requested, starting (once it runs) and running."""


def request_stop(s: Session, campaign: Campaign, actor: Actor, *, reason: str, ending: Ending) -> None:
    """Ask the daemon to stop ``campaign`` (``StopRequested``). The caller commits."""
    emit(s, StopRequested(reason=reason, ending=ending), TargetRef(TargetKind.CAMPAIGN, campaign.id), actor)


class CampaignClosed(EventModel):
    """The daemon closed a stopped campaign: it requested the closing sweeps, the ``stopped`` triggers.

    It closes a campaign once the runs that the stop wound down ended and their commits are judged (``settled``), so
    the closing review sees the final head. Stop triggers with ``while_held = "skip"`` close it at the stop, with no sweep.
    """

    READ_BY: ClassVar[ReadBy] = ReadBy.READERS
    kind: Literal['campaign.closed'] = 'campaign.closed'


def unclosed(s: Session, campaign: Campaign) -> bool:
    """Whether the daemon stopped ``campaign`` after it last closed it (``CampaignClosed``).

    A ``stopped`` that someone set by hand (an UPDATE of ``campaigns.status``) is not the daemon's stop: it gets no
    closing sweep.
    """
    on_campaign = Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id)
    last = select(func.max(Event.id)).where(on_campaign)
    stopped = s.scalar(last.where(Event.actor == DAEMON_ACTOR, has(CampaignStatusChanged, new=CampaignStatus.STOPPED)))
    closed = s.scalar(last.where(has(CampaignClosed)))
    return stopped is not None and stopped > (closed or 0)


def request_cancel(s: Session, target: Run | AgentSession, actor: Actor, *, ending: Ending) -> None:
    """Ask the daemon to cancel a run or an agent (``CancelRequested``). The caller commits."""
    match target:
        case Run():
            on = TargetRef(RUN, target.id)
            cancel = CancelRequested(kind='run.cancel_requested', ending=ending)
        case AgentSession():
            on = TargetRef(TargetKind.AGENT_SESSION, target.id)
            cancel = CancelRequested(kind='agent_session.cancel_requested', ending=ending)
        case _:
            assert_never(target)
    emit(s, cancel, on, actor)


def cancelled(ask: Event, cancel: CancelRequested) -> ColumnElement[bool]:
    """Select the agents that a cancel acts on: a run's agents, or an agent and the other agents of its run."""
    match cancel.kind:
        case 'run.cancel_requested':
            return working_on(RunId(ask.target_id))
        case 'agent_session.cancel_requested':
            run = (
                select(AgentSession.subject_id)
                .where(AgentSession.id == ask.target_id, AgentSession.subject_kind == RUN)
                .scalar_subquery()
            )
            return (AgentSession.id == ask.target_id) | (working_on(run))
        case _:
            assert_never(cancel.kind)


def run_cancel(s: Session, run_id: RunId) -> tuple[Event, CancelRequested] | None:
    """Return the latest cancel of a run, open or answered: requested on the run, or on any of its agents, or None.

    A cancel on an agent holds for its run, so an implementer that a relaunch puts in the place of one that failed to start
    is cancelled too.
    """
    agents = select(AgentSession.id).where(working_on(run_id))
    on_agent = (Event.target_kind == TargetKind.AGENT_SESSION) & Event.target_id.in_(agents)
    return latest(s, CancelRequested, (Event.target == TargetRef(RUN, run_id)) | on_agent)


def agent_cancel(s: Session, agent_session: AgentSession) -> tuple[Event, CancelRequested] | None:
    """Return the latest cancel of an agent, or None: its run's (``run_cancel``) for an implementer, its own for a sweep."""
    if (run := run_of(agent_session)) is not None:
        return run_cancel(s, run)
    return latest(s, CancelRequested, Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session.id))


ACTING = (AgentSessionState.REQUESTED, AgentSessionState.STARTING, AgentSessionState.RUNNING)
"""The states of an agent that a cancel still has to act on. An ``ending`` one is ended already."""


def answer_done(s: Session) -> None:
    """Answer each open stop and cancel that the daemon carried out. The caller commits.

    A stop is carried out once its campaign is stopped and, with ``now``, none of the campaign's implementers is starting
    or running. A cancel is carried out once none of its agents (``cancelled``) is requested, starting or running: the
    daemon closed or ended each one, or it ended on its own.
    """
    for asked, stop in pending(s, StopRequested, Event.target_id.in_(select(Campaign.id))):
        campaign = s.get_one(Campaign, asked.target_id)
        if campaign.status is not CampaignStatus.STOPPED:
            continue
        match stop.ending:
            case Ending.WRAP_UP:
                done = True
            case Ending.NOW:
                running = (AgentSessionState.STARTING, AgentSessionState.RUNNING)
                implementers = select(AgentSession).where(
                    AgentSession.campaign_id == campaign.id,
                    (AgentSession.subject_kind == RUN),
                    AgentSession.state.in_(running),
                )
                done = not count(s, implementers)
            case _:
                assert_never(stop.ending)
        if done:
            answer(s, asked)
    for asked, cancel in pending(s, CancelRequested):
        if not count(s, select(AgentSession).where(cancelled(asked, cancel), AgentSession.state.in_(ACTING))):
            answer(s, asked)


def check(s: Session, campaign: Campaign, cfg: CampaignConfig) -> Go | Hold | Stop | Escalate:
    """Ask the campaign's stop rule whether ``campaign`` goes on, holds, or stops now.

    The rule is ``[plugins.stop_rule]``, else ``limits``. Only the facts that it reads are gathered
    (``StopRule.reads``). ``head`` is the first track's primary at the head: config validation rejects a limit on it
    for a campaign with more than one track. A stop request is not the rule's: see ``requested``.
    """
    rule = STOP_RULES.selected(cfg.plugins)
    return rule.check(_case(s, campaign, cfg, utcnow()))


def describe(s: Session, campaign: Campaign, cfg: CampaignConfig, now: datetime) -> list[str]:
    """Ask the campaign's stop rule where the campaign stands against it (``StopRule.describe``)."""
    return STOP_RULES.selected(cfg.plugins).describe(_case(s, campaign, cfg, now))


def _case(s: Session, campaign: Campaign, cfg: CampaignConfig, now: datetime) -> StopCase:
    """The case of the campaign's stop rule: the facts that it reads, gathered as of ``now``."""
    primary = cfg.all_tracks[0].primary
    reads = STOP_RULES.selected(cfg.plugins).reads()
    return StopCase(primary.name, primary.direction, gather(s, campaign, cfg, reads, now))


def left(s: Session, campaign: Campaign, cfg: CampaignConfig) -> Facts:
    """What the campaign's stop rule leaves of each fact now (``Go.left``).

    Nothing bounds a campaign that the rule holds or stops, or that piloop does not ask it about (``asked``).
    """
    return left_of(check(s, campaign, cfg)) if asked(s, campaign, cfg) else Facts()


def left_of(answer: Go | Hold | Stop | Escalate) -> Facts:
    """What an answer of the stop rule leaves of each fact: ``Go.left``, also as the ``then`` of an ``Escalate``."""
    match done := acted(answer):
        case Go(left=facts):
            return facts
        case Hold() | Stop():
            return Facts()
        case _:
            assert_never(done)


GOES_ON = 'The campaign goes on under the new rule.'
WHAT_THEN: dict[type[Go | Hold | Stop], str] = {
    Go: 'The campaign goes on as it is.',
    Hold: 'Nothing new starts until the rule lets it.',
    Stop: 'The campaign stops.',
}
"""What keeping the stop rule does, by what the rule does after its escalation (``Escalate.then``)."""


def acted(answer: Go | Hold | Stop | Escalate) -> Go | Hold | Stop:
    """What piloop does on an answer of the stop rule: the answer, or the ``then`` of an ``Escalate``."""
    return answer.then if isinstance(answer, Escalate) else answer


def escalated(
    s: Session, campaign: Campaign, cfg: CampaignConfig, answer: Go | Hold | Stop | Escalate
) -> Go | Hold | Stop:
    """Act on an ``Escalate``: post its escalation, once for each distinct reason, and return its ``then``.

    The caller commits. The escalation is the record: a reason that one of the campaign's escalations already says posts nothing.
    """
    match answer:
        case Escalate(reason=reason, then=then):
            rule = STOP_RULES.name_of(STOP_RULES.selected(cfg.plugins))
            asked = EscalationView(
                headline='The stop rule needs your decision before the campaign goes on.',
                why=reason,
                choices=(
                    Choice(id='A', label='Change the rule in piloop.toml, then run piloop init.', result=GOES_ON),
                    Choice(id='B', label='Keep the rule as it is.', result=WHAT_THEN[type(then)]),
                ),
                details=f'Stop rule {rule}: {reason}',
            )
            escalations.escalate_once(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), asked, priority=0)
            return then
        case Go() | Hold() | Stop():
            return answer
        case _:
            assert_never(answer)


def gather(
    s: Session, campaign: Campaign, cfg: CampaignConfig, reads: Collection[Fact[FactValue]], now: datetime
) -> Facts:
    """Gather the facts in ``reads`` from the campaign's fact sources (``[plugins.fact_source] use``), as of ``now``. Most cost queries."""
    wanted = frozenset(reads)
    values: dict[str, FactValue] = {}
    for source in cfg.fact_sources():
        if mine := wanted & set(source.supplies()):
            values |= {fact.name: value for fact, value in source.gather(s, campaign, mine, now).items()}
    return Facts(values)


def timeout(
    s: Session, campaign: Campaign, cfg: CampaignConfig, role_timeout: float, started_at: datetime
) -> float | Stop:
    """Return the seconds that an agent of ``campaign`` that started at ``started_at`` may run, or the rule's ``Stop``.

    The seconds are its ``role_timeout``, cut where the stop rule's ``hours`` run out (``Go.left``): a run that starts
    minutes before the stop ends with it. A stopped campaign cuts nothing, so its closing review has its whole timeout.
    ``Stop``: the rule stops the active campaign now. An agent that has not started never starts: its row is requested
    again, and the pass that stops the campaign closes it. One that runs is wound down by that pass.
    """
    if campaign.status is not CampaignStatus.ACTIVE:
        return role_timeout
    now = utcnow()
    match answer := acted(STOP_RULES.selected(cfg.plugins).check(_case(s, campaign, cfg, now))):
        case Go(left=facts):
            hours = facts.get(HOURS)
        case Stop():
            return answer
        case Hold():
            hours = None
        case _:
            assert_never(answer)
    if hours is None:
        return role_timeout
    elapsed = (now - started_at).total_seconds()
    return max(0.0, min(role_timeout, elapsed + hours * 3600))


def seconds(timeout: float | Stop) -> float:
    """The seconds of a ``timeout`` for an agent that already runs: none once the rule stops the campaign."""
    return 0.0 if isinstance(timeout, Stop) else timeout


def requested(s: Session, campaign: Campaign) -> Stop | None:
    """Return the stop that someone requested and the daemon has not carried out, or None.

    It wins over every answer of the rule. ``answer_done`` answers it once the campaign is stopped.
    """
    asks = pending(s, StopRequested, Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id))
    if not asks:
        return None
    asked, stop = asks[0]
    return Stop(f'stopped by {asked.actor}: {stop.reason}' if stop.reason else f'stopped by {asked.actor}')


def review_waits(s: Session, campaign: Campaign, cfg: CampaignConfig) -> bool:
    """Whether the stopped campaign's closing review waits for the hold to end.

    That is ``while_held = "wait"``, with an agent still requested. piloop asks the stop rule about such a campaign, and
    reads its facts, so that the hold can end.
    """
    if (
        campaign.status is not CampaignStatus.STOPPED
        or STOP_RULES.selected(cfg.plugins).while_held is not WhileHeld.WAIT
    ):
        return False
    requested = select(AgentSession.id).where(
        AgentSession.campaign_id == campaign.id, AgentSession.state == AgentSessionState.REQUESTED
    )
    return s.scalar(requested.limit(1)) is not None


def review_skipped(s: Session, campaign: Campaign, held: str) -> None:
    """Post the escalation of a closing review that ``while_held = "skip"`` dropped. The caller commits."""
    asked = EscalationView(
        headline='The campaign stopped without its closing review. Do you want a review?',
        why='The campaign stopped while the stop rule held its launches. The rule skips the review then.',
        choices=(
            Choice(id='A', label='Start the review by hand.', result='An agent reviews the campaign and reports.'),
            Choice(id='B', label='Do nothing.', result='The campaign stays stopped without a review.'),
        ),
        details=(
            f'Closing review skipped: the campaign stopped while the stop rule held its launches ({held}), and the'
            ' rule skips a review then (while_held = "skip").'
        ),
    )
    escalations.escalate(s, TargetRef(TargetKind.CAMPAIGN, campaign.id), asked, author=System.DAEMON, priority=0)


def asked(s: Session, campaign: Campaign, cfg: CampaignConfig) -> bool:
    """Whether piloop asks the campaign's stop rule now.

    It asks about an active campaign, and about a stopped one whose closing review waits for the hold to end
    (``review_waits``).
    """
    return campaign.status is CampaignStatus.ACTIVE or review_waits(s, campaign, cfg)


def held(s: Session, campaign: Campaign, cfg: CampaignConfig) -> str | None:
    """Return why the stop rule holds the campaign's launches and evaluations now, or None.

    A campaign that piloop does not ask about (``asked``) is not held.
    """
    if not asked(s, campaign, cfg):
        return None
    answer = acted(check(s, campaign, cfg))
    return answer.reason if isinstance(answer, Hold) else None


def _over_runs[T](s: Session, campaign: Campaign, aggregate: ColumnElement[T], *where: ColumnElement[bool]) -> T | None:
    return s.scalar(campaign_runs(campaign.id).with_only_columns(aggregate).where(*where))


def _since_reopen(
    s: Session, campaign: Campaign, at: InstrumentedAttribute[datetime] | InstrumentedAttribute[datetime | None]
) -> tuple[ColumnElement[bool], ...]:
    """The condition that ``at`` comes after the campaign's latest reopen (``reopened_at``): none if it never was."""
    since = reopened_at(s, campaign)
    return () if since is None else (at > since,)


def hours_due(campaign: Campaign, answer: Go | Hold | Stop | Escalate, now: datetime) -> datetime | None:
    """When the ``hours`` that the stop rule's answer leaves run out, for a campaign that runs and is not paused.

    None when the answer leaves no hours, or while time does not count. The pass then checks the rule.
    """
    hours = left_of(answer).get(HOURS)
    if hours is None or campaign.status is not CampaignStatus.ACTIVE or campaign.paused is not None:
        return None
    return now + timedelta(hours=max(hours, 0))


def hours(s: Session, campaign: Campaign, now: datetime) -> float | None:
    """Return the hours from the start of the campaign's first run to ``now``, or None if no run started.

    The time that the campaign was paused (``piloop pause``) does not count, so a pause does not use up ``max_hours``.
    The pauses are the ``campaign.paused`` events of the change log. A reopened campaign counts from its first run
    since the latest reopen.
    """
    first = _over_runs(s, campaign, func.min(Run.started_at), *_since_reopen(s, campaign, Run.started_at))
    return None if first is None else (now - first - paused_for(s, campaign, first, now)) / timedelta(hours=1)


def paused_for(s: Session, campaign: Campaign, since: datetime, now: datetime) -> timedelta:
    """Return how long the campaign was paused between ``since`` and ``now``."""
    changes = in_order(s, CampaignPausedChanged, Event.target == TargetRef(TargetKind.CAMPAIGN, campaign.id))
    total, start = timedelta(), None
    for at, new in ((e.at, change.new) for e, change in changes):
        if new is not None and start is None:
            start = at
        elif new is None and start is not None:
            total += max(at - max(start, since), timedelta())
            start = None
    if start is not None:
        total += max(now - max(start, since), timedelta())
    return total
