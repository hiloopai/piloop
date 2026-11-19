"""Replay the state graph of the best fold model (``tests/models/best_fold.tla``) against the real pool and judge.

TLC wrote the graph of ``best_fold_traces.cfg``, the fixed design with one judge (``-dump dot,actionlabels``), and
``scripts/graph_traces.py`` turned it into traces that take every kind of transition that the driver checks, once at
least (``--by best,waitc,err,judge --keep op,arg,best,waitc,err,judge,gen,store``; ``tests/traces/best_fold``). Each
trace has two candidates of its own in one campaign, and a best rule whose answers the trace gives. Each step runs what
the model's action cites: a ready a measurement that confirms the candidate, a remeasure one more, an exclusion
``excluded_reason``, a rule change a new ``version`` of the rule's table. A start runs ``judging.judge`` on a thread of
its own, which the replay holds before each of its reads: a read lets it read and ask the rule, and an answer is the
rule's. After each step the best, the candidate that the fold waits at, whether the rule did not answer there, and the
stored answers are the model's.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import ClassVar

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation.tables import Measurement
from piloop.plugins.campaign_type.kit.pool import judging, pool as pool_module
from piloop.plugins.campaign_type.kit.pool.best_rule.contract import BestRule, Case, Keep, Refuse, Unanswered
from piloop.plugins.campaign_type.kit.pool.pool import Pool
from piloop.plugins.campaign_type.kit.pool.tables import Judgement
from tests.conftest import baseline, home_cas
from tests.daemon.test_pool import configure
from tests.test_lease_model import State, _steps

TRACES = Path(__file__).parent / 'traces' / 'best_fold'
SCORES = {1: (2.0, 0.0), 2: (3.0, 1.0)}
"""What each evaluation of a candidate of the model scores: its first, then its remeasure. The baseline scores 1."""
ANSWERS: dict[str, Callable[[], Keep | Refuse | Unanswered]] = {
    'keep': Keep,
    'refuse': lambda: Refuse('the model refuses it'),
    'unanswered': lambda: Unanswered('the model does not answer'),
}
WAIT = 30
"""Seconds to wait for the judge's thread: a step that never comes is a failure, not a hang."""

type Event = tuple[str, Case | None]


@dataclass
class Channel:
    """Between the replay and the judge's thread: what the thread does next, and what lets it go on."""

    events: Queue[Event] = field(default_factory=Queue)
    """``read``: it waits to read; ``asked``: the rule has the case; ``done``: the judge returned."""
    reads: Queue[None] = field(default_factory=Queue)
    answers: Queue[Keep | Refuse | Unanswered] = field(default_factory=Queue)

    def next(self) -> Event:
        return self.events.get(timeout=WAIT)


class Scripted(BestRule):
    """A best rule that hands each case to the replay, and answers what the replay says: the model's answer."""

    version: int
    """Its table: a new version is a rule change."""
    channel: ClassVar[Channel | None] = None

    def judge(self, case: Case) -> Keep | Refuse | Unanswered:
        assert Scripted.channel is not None
        Scripted.channel.events.put(('asked', case))
        return Scripted.channel.answers.get(timeout=WAIT)


@dataclass
class Replay:
    """One trace: its two candidates, the rule's version, and the judge's thread while it runs."""

    db: Database
    campaign: Campaign
    seq: int
    ids: dict[int, CandidateId] = field(default_factory=dict)
    """The real candidate of each candidate of the model; the model's 0 is the baseline."""
    version: int = 0
    channel: Channel = field(default_factory=Channel)
    thread: threading.Thread | None = None
    sessions: int = 0
    """The sessions that the judge's thread opened: each even one is a read."""
    held: str | None = None
    """Where the judge's thread waits for the replay: ``read`` or ``asked``."""
    failed: list[BaseException] = field(default_factory=list)

    def start(self) -> None:
        self.version = 10 * self.seq
        base = baseline(self.db, self.campaign.id).id
        with self.db.session() as s:
            for c in (1, 2):
                tree = f'fold-{self.seq}-{c}'
                made = Candidate(
                    campaign_id=self.campaign.id, parent_id=base, tree_sha=tree, revision=tree, origin=Origin.RUN
                )
                s.add(made)
                s.flush()
                self.ids[c] = made.id
            s.commit()
        self.ids[0] = base
        Scripted.channel = self.channel

    def session(self, original: Callable[[], Session]) -> Session:
        """``Database.session``, held before each read of the judge's thread until the model reads."""
        if threading.current_thread() is self.thread:
            if self.sessions % 2 == 0:
                self.channel.events.put(('read', None))
                self.channel.reads.get(timeout=WAIT)
            self.sessions += 1
        return original()

    def step(self, state: State) -> None:
        match state['arg']:
            case (str(), int(c), str(said)):
                pass
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        match state['op']:
            case 'ready':
                self._measure(c, SCORES[c][0])
            case 'remeasure':
                self._measure(c, SCORES[c][1])
            case 'exclude':
                with self.db.session() as s:
                    candidate = s.get_one(Candidate, self.ids[c])
                    candidate.excluded_reason, candidate.excluded_by = 'the model', 'human'
                    s.commit()
            case 'rule':
                self.version += 1
            case 'start':
                self.sessions = 0
                self.thread = threading.Thread(target=self._judge, name=f'judge-{self.seq}', daemon=True)
                self.thread.start()
                assert self._next()[0] == 'read', 'it reads first'
            case 'read':
                self.channel.reads.put(None)
                match self._next():
                    case ('asked', Case() as case):
                        assert c != 0, 'the model reads that nothing waits'
                        assert (case.candidate.id, case.incumbent.id) == (self.ids[c], self.ids[_int(state, 'best')])
                    case ('done', _):
                        assert c == 0, 'the model asks the rule'
                        self._joined()
                    case other:
                        raise AssertionError(f'the judge did {other[0]}')
            case 'answer':
                self.channel.answers.put(ANSWERS[said]())
                after = self._next()[0]
                assert after == ('done' if said == 'unanswered' else 'read'), after
                if after == 'done':
                    self._joined()
            case other:
                raise AssertionError(f'not a step of the model: {other}')
        self.check(state)

    def _next(self) -> Event:
        """The judge's next event: where it waits now, or that it returned."""
        event = self.channel.next()
        self.held = None if event[0] == 'done' else event[0]
        return event

    def _judge(self) -> None:
        try:
            judging.judge(self.db, home_cas(self.db), self.campaign.id)
        except BaseException as e:  # the thread's last frame: the replay raises it
            self.failed.append(e)
        finally:
            self.channel.events.put(('done', None))

    def _joined(self) -> None:
        assert self.thread is not None
        self.thread.join(WAIT)
        assert self.failed == [], self.failed
        self.thread = None

    def _measure(self, c: int, score: float) -> None:
        with self.db.session() as s:
            evaluator = s.get_one(Campaign, self.campaign.id).evaluator_id
            assert evaluator is not None
            s.add(Measurement(candidate_id=self.ids[c], evaluator_id=evaluator, metrics={'score': score}))
            s.commit()

    def check(self, state: State) -> None:
        """The best, the step that the fold waits at, and the stored answers are the model's."""
        with self.db.session() as s:
            campaign = s.get_one(Campaign, self.campaign.id)
            pool = Pool.of(s, campaign, campaign.config.all_tracks[0])
            stored = s.scalar(
                select(func.count()).select_from(Judgement).where(Judgement.candidate_id.in_(self.ids.values()))
            )
        fold = pool.fold
        assert pool.best() == self.ids[_int(state, 'best')], 'the best'
        waiting = fold.waiting.candidate if fold.waiting is not None else None
        assert waiting == (self.ids[_int(state, 'waitc')] if _int(state, 'waitc') else None), 'the step it waits at'
        assert (fold.error is not None) == state['err'], 'whether the rule did not answer'
        match state['store']:
            case {'#map': tuple(pairs)}:
                assert stored == len(pairs), 'the stored answers'
            case _:
                assert stored == 0, 'no answer stored'

    def end(self) -> None:
        """Let the judge return, then delete the trace's candidates: their measurements and answers go with them."""
        while self.thread is not None:
            match self.held:
                case 'read':
                    self.channel.reads.put(None)
                case 'asked':
                    self.channel.answers.put(Unanswered('the replay ended'))
            if self._next()[0] == 'done':
                self._joined()
        ours = [self.ids[1], self.ids[2]]
        with self.db.session() as s:
            s.execute(delete(Judgement).where(Judgement.candidate_id.in_(ours)))
            s.execute(delete(Measurement).where(Measurement.candidate_id.in_(ours)))
            s.execute(delete(Candidate).where(Candidate.id.in_(ours)))
            s.commit()


def _int(state: State, name: str) -> int:
    value = state[name]
    assert isinstance(value, int) and not isinstance(value, bool)
    return value


def test_the_best_is_the_fold_over_the_rule_s_answers_on_every_transition_of_its_graph(
    db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(db, campaign, evaluator={'noise_runs': 1})
    paths = sorted(TRACES.glob('*.itf.json'))
    assert paths
    original = db.session
    for seq, path in enumerate(paths):
        replay = Replay(db, campaign, seq)
        monkeypatch.setattr(pool_module, 'best_rule', lambda _cfg, r=replay: Scripted(version=r.version))
        monkeypatch.setattr(db, 'session', lambda r=replay: r.session(original))
        replay.start()
        for action, state in _steps(path)[1:]:
            if action != 'Done':
                try:
                    replay.step(state)
                except AssertionError as e:
                    raise AssertionError(f'{path.name}, at {action}: {e}') from e
        replay.end()
