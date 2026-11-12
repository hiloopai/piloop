"""The pool: every submission is scored, a first score that beats the best is confirmed, and the best is a query."""

from contextlib import closing
from pathlib import Path

import pytest
from git import Repo
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from piloop.actor import Agent, System
from piloop.campaign.ops import Refused
from piloop.config import load_campaign_config
from piloop.db.events import in_order
from piloop.db.models import AgentSession, Campaign, Event
from piloop.db.session import Database
from piloop.ids import Revision, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog import ops as backlog_ops
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.ops import BaseMoved
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.evaluation import queue
from piloop.plugins.campaign_type.kit.evaluation.ids import EvaluatorId, Measured
from piloop.plugins.campaign_type.kit.evaluation.measure import (
    current_evaluator,
    record,
    request_remeasure,
    sealed_evaluator,
    sealed_of,
    status,
)
from piloop.plugins.campaign_type.kit.evaluation.submissions import submit, waiting
from piloop.plugins.campaign_type.kit.evaluation.tables import Cancel, Measurement
from piloop.plugins.campaign_type.kit.pool.due import due, firsts
from piloop.plugins.campaign_type.kit.pool.pool import Pool, best_of, branching, carried
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER, RUN, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run, working_on
from piloop.plugins.executor.workers import Workers
from piloop.plugins.vcs.git import Git
from tests.conftest import EVALUATE, ScriptRuntime, bare_config, baseline, factory, reports, serving

SUBMIT = {'noise_runs': 3}
"""Each candidate whose first score beats the best's is evaluated until it has 3 evaluations."""


def configure(db: Database, campaign: Campaign, **overrides: object) -> None:
    with db.session() as s:
        s.get_one(Campaign, campaign.id).config = load_campaign_config(Path(campaign.path), **overrides)
        s.commit()


def controller(db: Database, runtime: ScriptRuntime, tmp_path: Path) -> RunController:
    return serving(RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers()))


def scores(tmp_path: Path, *values: int) -> list[str]:
    """An evaluate command that reports the next score of ``values`` on each run, as a noisy timing would."""
    (tmp_path / 'scores').write_text(''.join(f'{v}\n' for v in values))
    take = f'v=$(head -1 {tmp_path}/scores); tail -n +2 {tmp_path}/scores > {tmp_path}/rest; mv {tmp_path}/rest {tmp_path}/scores'
    return ['sh', '-c', f'{take}; {reports(score="$v", size="1")}']


def lines(name: str) -> list[str]:
    """A sealed command that scores the number of lines of ``solve.txt`` times 10, minus ``name``'s number if present."""
    return ['sh', '-c', reports(score=f'$(( $(wc -l < solve.txt) * 10 - $(cat {name} 2>/dev/null || echo 0) ))')]


def running(db: Database, campaign: Campaign, *, claimed: bool = True) -> RunId:
    """A running run from the baseline, with its implementer, which claimed an idea unless not ``claimed``."""
    base = baseline(db, campaign.id).id
    with db.session() as s:
        n = len(s.scalars(select(Idea.id)).all())
        idea = Idea(
            track='score',
            campaign_id=campaign.id,
            title=f'more lines {n}',
            hypothesis='lines are good',
            created_by='test',
            status=IdeaStatus.CLAIMED if claimed else IdeaStatus.OPEN,
        )
        s.add(idea)
        s.flush()
        run = Run(
            campaign_id=campaign.id,
            idea_id=idea.id if claimed else None,
            attempt=1,
            base_candidate_id=base,
            harness='script',
            model='test-model',
            status=RunStatus.RUNNING,
        )
        s.add(run)
        s.flush()
        s.add(
            AgentSession(
                kind=IMPLEMENTER,
                campaign_id=campaign.id,
                subject=TargetRef(RUN, run.id),
                harness='script',
                model='test-model',
                reason='a free slot',
            )
        )
        s.commit()
        return run.id


def implementer(db: Database, run_id: RunId) -> Agent:
    with db.session() as s:
        agent = s.scalars(select(AgentSession).where(working_on(run_id))).one()
        return Agent(role='implementer', session=agent.id)


def checkout(campaign: Campaign, tmp_path: Path, name: str = 'checkout', at: str = 'HEAD') -> Path:
    path = tmp_path / name
    Repo(campaign.path).git.worktree('add', '--detach', str(path), at)
    return path


def commit(path: Path, text: str) -> Revision:
    """Write ``text`` to ``solve.txt`` and commit: the score of the tree is its number of lines."""
    (path / 'solve.txt').write_text(text)
    repo = Repo(path)
    repo.git.commit('-qam', f'{text.count(chr(10))} lines')
    return Revision(repo.head.commit.hexsha)


def submitted(db: Database, campaign: Campaign, run_id: RunId, revision: Revision) -> CandidateId:
    agent = implementer(db, run_id)
    with db.session() as s, closing(Git().open(Path(campaign.path))) as repo:
        found = submit(s, s.get_one(Run, run_id), repo.version(revision), repo, agent)
        s.commit()
        return found.id


def evaluations(db: Database, campaign: Campaign, candidate: CandidateId) -> tuple[int, int]:
    """How many visible and how many sealed evaluations ``candidate`` has."""
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        visible, hidden = p.evaluator_id, sealed_of(s, p, p.config)
        rows = s.scalars(select(Measurement).where(Measurement.candidate_id == candidate)).all()
        return sum(m.evaluator_id == visible for m in rows), sum(
            hidden is not None and m.evaluator_id == hidden.id for m in rows
        )


def pool(db: Database, campaign: Campaign) -> Pool:
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        return Pool.of(s, p, p.config.all_tracks[0])


def unmeasured(s: Session, campaign: Campaign) -> list[CandidateId]:
    """What runs wait for (``firsts``) that the current evaluator has not answered yet, nor anyone cancelled."""
    return [c.id for c in firsts(s, campaign) if not status(c, campaign.evaluator_id).settled]


def told(db: Database, candidate: CandidateId) -> str:
    with db.session() as s:
        [*_, (_, last)] = [e for e in in_order(s, CandidateScored) if e[0].target_id == candidate]
        return last.told


def test_a_submission_that_does_not_beat_the_best_gets_one_evaluation_and_no_sealed_one(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, evaluator={**SUBMIT, 'sealed': lines('none')})
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)  # 3 equal evaluations: a deterministic evaluator
    worse = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), ''))
    ex.score_submission(worse)
    assert evaluations(db, campaign, worse) == (1, 0)
    assert pool(db, campaign).best() == baseline(db, campaign.id).id
    assert told(db, worse) == 'score 0 over 1 eval; vs parent -1, vs best candidate#1 -1'


def test_a_first_score_that_beats_the_best_is_confirmed_to_noise_runs_and_scored_on_the_sealed_split(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The baseline has one evaluation, so the evaluator is not known to be deterministic: 3 confirm."""
    configure(db, campaign, evaluator={**SUBMIT, 'sealed': lines('none')})
    ex = controller(db, runtime, tmp_path)
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\nb\n'))
    ex.score_submission(better)
    assert evaluations(db, campaign, better) == (3, 1)
    assert evaluations(db, campaign, baseline(db, campaign.id).id)[1] == 1, 'the sealed split scored the baseline'
    assert pool(db, campaign).best() == better
    assert told(db, better) == 'score 3 over 3 evals: the best of the score track now, rank 1 of 2; vs parent +2'


@pytest.mark.parametrize('order', [('a', 'b'), ('b', 'a')])
def test_two_candidates_confirmed_in_either_order_give_the_same_best(
    db: Database, campaign: Campaign, order: tuple[str, str]
) -> None:
    """No order of the evaluations decides the best: it is the best mean among those that may be best."""
    configure(db, campaign, evaluator={'noise_runs': 2})
    means = {'a': [5.0, 5.0], 'b': [4.0, 6.0, 4.0]}
    ids: dict[str, CandidateId] = {}
    first = baseline(db, campaign.id)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, p, p.config).id
        for name in order:
            c = Candidate(campaign_id=p.id, parent_id=first.id, tree_sha=name, revision=name, origin=Origin.RUN)
            s.add(c)
            s.flush()
            ids[name] = c.id
            s.add_all(Measurement(candidate_id=c.id, evaluator_id=evaluator, metrics={'score': v}) for v in means[name])
        s.commit()
    found = pool(db, campaign)
    assert found.best() == ids['a'] and found.ranked == [ids['a'], ids['b'], first.id]


def test_a_lucky_first_score_that_falls_short_once_confirmed_leaves_the_best(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The baseline scores 9, 10, 11. The candidate's first score, 13, beats its mean of 10, so it is evaluated twice
    more: 8 and 8. Its mean of 9.67 does not beat the baseline's: the best stays."""
    configure(db, campaign, evaluate=scores(tmp_path, 9, 10, 11, 13, 8, 8), evaluator=SUBMIT)
    with db.session() as s:
        s.execute(delete(Measurement))
        s.commit()
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    lucky = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(lucky)
    found = pool(db, campaign)
    assert evaluations(db, campaign, lucky) == (3, 0)
    assert found.best() == baseline(db, campaign.id).id
    assert found.ranked == [baseline(db, campaign.id).id, lucky], 'confirmed: it may be best, but its mean is lower'
    assert told(db, lucky).startswith('score 9.667 over 3 evals: rank 2 of 2; vs parent -0.3333')


def test_a_confirmed_candidate_that_fails_the_sealed_split_or_is_excluded_is_never_best(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The sealed command takes 25 off a tree that has ``gamed``: it scores 5 there, below the baseline's 10."""
    configure(db, campaign, evaluator={**SUBMIT, 'sealed': lines('gamed')})
    ex = controller(db, runtime, tmp_path)
    path = checkout(campaign, tmp_path)
    (path / 'gamed').write_text('25\n')
    Repo(path).git.add('gamed')
    gamed = submitted(db, campaign, running(db, campaign), commit(path, 'base\na\nb\n'))
    ex.score_submission(gamed)
    found = pool(db, campaign)
    assert found.best() == baseline(db, campaign.id).id
    assert found.refusal(gamed) == 'it does not beat the baseline on the sealed split (5 vs 10)'
    honest = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path, 'two'), 'base\na\n'))
    ex.score_submission(honest)
    assert pool(db, campaign).best() == honest
    with db.session() as s:
        s.get_one(Candidate, honest).excluded_reason, s.get_one(Candidate, honest).excluded_by = 'gamed', 'human'
        s.commit()
    assert pool(db, campaign).best() == baseline(db, campaign.id).id


def test_a_crashed_sealed_evaluation_is_the_split_s_answer_so_what_waits_behind_it_is_measured(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A crash of the sealed command is its answer, as a visible crash is: the daemon does not run it again each pass.

    ``due`` serves its first item each pass, so a candidate that stayed due for ever kept the reference from its turn.
    """
    configure(db, campaign, evaluator={'noise_runs': 1, 'sealed': ['sh', '-c', 'exit 1']})
    ex = controller(db, runtime, tmp_path)
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(better)
    with db.session() as s:
        reference = Candidate(campaign_id=campaign.id, tree_sha='f' * 40, revision='f' * 40, origin=Origin.REFERENCE)
        s.add(reference)
        s.commit()
        p = s.get_one(Campaign, campaign.id)
        hidden = sealed_of(s, p, p.config)
        assert hidden is not None
        assert status(s.get_one(Candidate, better), hidden.id).state is Measured.CRASHED
        assert [c.id for c in due(s, p)] == [reference.id], 'the crashed seal waits for no second run'
    assert evaluations(db, campaign, better) == (1, 1)
    assert pool(db, campaign).refusal(better) is not None, 'without a sealed score it may not be best'


def test_a_baseline_whose_evaluate_crashes_before_its_first_rung_is_due_until_noise_runs_crashes(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A crash before the first rung's line is stored at that rung. It is a crash all the same: the baseline is due
    until ``noise_runs`` of its evaluations crashed, as without a ladder, and runs no longer wait for it."""
    ladder = [{'name': 'screen', 'stop': {'score': {'floor': -1000}}}]
    configure(db, campaign, evaluate=['sh', '-c', 'exit 3'], ladder=ladder, evaluator={'noise_runs': 3})
    ex = controller(db, runtime, tmp_path)
    base = baseline(db, campaign.id).id
    passes = 0
    for _ in range(5):
        with db.session() as s:
            if base not in [c.id for c in due(s, s.get_one(Campaign, campaign.id))]:
                break
        ex.remeasure(base)
        passes += 1
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        now = status(s.get_one(Candidate, base), p.evaluator_id)
        assert (now.state, now.crashes, now.rung, passes) == (Measured.CRASHED, 3, 'screen', 3)
        assert unmeasured(s, p) == [], 'its crash answers it: runs do not wait for it'


def test_a_claim_moves_a_run_that_the_best_passed_since_it_was_queued_to_the_best(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run queued from the baseline claims after a new best: it starts from the new best, and its checkout moves."""
    configure(db, campaign, evaluator={'noise_runs': 1})
    ex = controller(db, runtime, tmp_path)
    late, first = running(db, campaign, claimed=False), baseline(db, campaign.id).id
    agent = implementer(db, late)
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\nb\nc\n'))
    ex.score_submission(better)
    with db.session() as s:
        ideas = s.scalars(select(Idea.id).where(Idea.status == IdeaStatus.OPEN)).all()
        claim = backlog_ops.claim(s, ideas[0], actor=agent)
        s.commit()
        assert (claim.base, s.get_one(Run, late).base_candidate_id) == (better, better)
        [moved] = [e.payload for e in s.scalars(select(Event).where(Event.kind == 'run.base_moved'))]
        assert moved == BaseMoved(candidate=better, was=first)
    on_time = running(db, campaign, claimed=False)
    agent = implementer(db, on_time)
    with db.session() as s:
        s.get_one(Run, on_time).base_candidate_id = better
        idea = s.scalars(select(Idea.id).where(Idea.status == IdeaStatus.OPEN)).first()
        assert idea is not None
        assert backlog_ops.claim(s, idea, actor=agent).base is None, 'it starts from the best'


def test_a_claim_with_a_base_starts_the_run_there_and_its_submission_stands_against_parent_line_and_best(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """A run claims an idea with the base of a crash: its submission is that crash's child. A second claim, or a
    base after the claim, is refused."""
    configure(db, campaign, evaluator={'noise_runs': 1})
    ex = controller(db, runtime, tmp_path)
    first = running(db, campaign)
    better = submitted(db, campaign, first, commit(checkout(campaign, tmp_path), 'base\na\nb\nc\n'))
    ex.score_submission(better)
    crash = submitted(db, campaign, first, commit(checkout(campaign, tmp_path, 'crash'), 'base\nx\n'))
    with db.session() as s:
        s.add(Measurement(candidate_id=crash, evaluator_id=s.get_one(Campaign, campaign.id).evaluator_id, error='boom'))
        s.commit()
    run_id = running(db, campaign, claimed=False)
    agent = implementer(db, run_id)
    with db.session() as s:
        idea = s.scalars(select(Idea).where(Idea.status == IdeaStatus.OPEN)).one()
        backlog_ops.claim(s, idea.id, actor=agent, base=crash)
        s.commit()
        assert s.get_one(Run, run_id).base_candidate_id == crash
        with pytest.raises(Refused, match='a run chooses its base with its claim'):
            backlog_ops.claim(s, idea.id, actor=agent, base=better)
    path = checkout(campaign, tmp_path, 'branch', at=crash_revision(db, crash))
    child = submitted(db, campaign, run_id, commit(path, 'base\nx\ny\n'))
    ex.score_submission(child)
    with db.session() as s:
        assert s.get_one(Candidate, child).parent_id == crash
    assert told(db, child) == f'score 3 over 1 eval: rank 2 of 3; vs best candidate#{better} -1'
    lined = submitted(db, campaign, run_id, commit(path, 'base\nx\ny\nz\nw\n'))
    ex.score_submission(lined)
    assert told(db, lined) == (
        "score 5 over 1 eval: the best of the score track now, rank 1 of 4; vs parent +2, vs your line's best +1"
    )


def crash_revision(db: Database, candidate: CandidateId) -> str:
    with db.session() as s:
        return s.get_one(Candidate, candidate).revision


def test_a_deterministic_evaluator_confirms_with_one_evaluation(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, evaluator=SUBMIT)
    ex = controller(db, runtime, tmp_path)
    ex.remeasure(baseline(db, campaign.id).id)
    assert evaluations(db, campaign, baseline(db, campaign.id).id) == (3, 0), 'the baseline runs noise_runs times'
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(better)
    assert evaluations(db, campaign, better) == (1, 0), 'its runs agree: one evaluation confirms it'
    assert pool(db, campaign).best() == better


def test_the_brief_lists_the_best_and_the_best_of_three_other_lines_one_per_line(
    db: Database, campaign: Campaign
) -> None:
    """Five lines from the baseline. The best's own ancestor and a child of another line are left out."""
    configure(db, campaign, evaluator={'noise_runs': 1})
    first = baseline(db, campaign.id).id
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        evaluator = current_evaluator(s, p, p.config).id
        made: dict[str, CandidateId] = {}
        for name, parent, score in (
            ('a', None, 3.0),
            ('a2', 'a', 9.0),
            ('b', None, 8.0),
            ('b2', 'b', 7.0),
            ('c', None, 6.0),
            ('d', None, 5.0),
            ('e', None, 4.0),
        ):
            c = Candidate(
                campaign_id=p.id,
                parent_id=made[parent] if parent else first,
                tree_sha=name,
                revision=name,
                summary=f'line {name}',
                origin=Origin.RUN,
            )
            s.add(c)
            s.flush()
            made[name] = c.id
            s.add(Measurement(candidate_id=c.id, evaluator_id=evaluator, metrics={'score': score}))
        s.commit()
    assert pool(db, campaign).worth_branching() == [made['a2'], made['b'], made['c'], made['d']]
    with db.session() as s:
        shown = branching(s, s.get_one(Campaign, campaign.id))
    assert shown == [
        '- Other lines worth branching from, with `piloop idea claim <id> --base <candidate>`:',
        f'  - candidate#{made["b"]}: score 8 over 1 eval, -1 vs the best — line b',
        f'  - candidate#{made["c"]}: score 6 over 1 eval, -3 vs the best — line c',
        f'  - candidate#{made["d"]}: score 5 over 1 eval, -4 vs the best — line d',
    ]


def test_after_an_evaluator_change_runs_wait_for_the_baseline_and_the_old_best_and_the_best_stays_where_it_was(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """No candidate has a score under a new evaluator. The daemon measures the baseline and the best of the old one,
    runs wait meanwhile, and the old best is the best again once measured."""
    configure(db, campaign, evaluator={'noise_runs': 1})
    ex = controller(db, runtime, tmp_path)
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(better)
    assert pool(db, campaign).best() == better
    configure(db, campaign, evaluate=[*EVALUATE[:2], EVALUATE[2] + ' # v2'], evaluator={'noise_runs': 1})
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        current_evaluator(s, p, p.config)
        s.commit()
    first = baseline(db, campaign.id).id
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert carried(s, p) is not None and unmeasured(s, p) == [better, first]
        assert [c.id for c in due(s, p)] == [better, first]
    for c in (first, better):
        ex.remeasure(c)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert unmeasured(s, p) == [] and due(s, p) == []
    assert pool(db, campaign).best() == better


def test_a_reference_whose_first_score_beats_the_best_is_confirmed_and_can_be_best(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, evaluator={**SUBMIT, 'sealed': lines('none')})
    ex = controller(db, runtime, tmp_path)
    revision = commit(checkout(campaign, tmp_path), 'base\na\nb\n')
    with db.session() as s:
        tree = Repo(campaign.path).commit(revision).tree.hexsha
        reference = Candidate(campaign_id=campaign.id, tree_sha=tree, revision=revision, origin=Origin.REFERENCE)
        s.add(reference)
        s.commit()
        made = reference.id
    ex.remeasure(made)
    assert evaluations(db, campaign, made) == (3, 1)
    assert pool(db, campaign).best() == made


def test_one_evaluation_confirms_but_the_sealed_split_still_scores_what_beats_the_best_and_a_new_command_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """With ``noise_runs = 1`` a candidate is confirmed by its first evaluation: the sealed split scores it all the
    same. A new sealed command has no score of it: the daemon scores the would-be best there, and only there."""
    configure(db, campaign, evaluator={'noise_runs': 1, 'sealed': lines('none')})
    ex = controller(db, runtime, tmp_path)
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(better)
    assert evaluations(db, campaign, better) == (1, 1) and pool(db, campaign).best() == better
    configure(db, campaign, evaluator={'noise_runs': 1, 'sealed': lines('other')})
    assert pool(db, campaign).best() == baseline(db, campaign.id).id, 'the new sealed split has scored it not'
    with db.session() as s:
        assert [c.id for c in due(s, s.get_one(Campaign, campaign.id))] == [better]
    ex.remeasure(better)
    assert evaluations(db, campaign, better) == (1, 1), 'scored on the new split only'
    assert pool(db, campaign).best() == better


def test_a_submission_whose_evaluation_is_cancelled_waits_for_no_score(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    configure(db, campaign, evaluator={'noise_runs': 1})
    run_id = running(db, campaign)
    made = submitted(db, campaign, run_id, commit(checkout(campaign, tmp_path), 'base\na\n'))
    with db.session() as s:
        assert [c.id for c in waiting(s, s.get_one(Campaign, campaign.id))] == [made]
        evaluator = s.get_one(Campaign, campaign.id).evaluator_id
        assert evaluator is not None
        queue.request(s, made, evaluator)
        queue.cancel(s, made, System.HUMAN)
        s.commit()
        assert s.get_one(Candidate, made).origin is Origin.RUN, 'a cancel keeps the origin'
        assert waiting(s, s.get_one(Campaign, campaign.id)) == []


def cancel_running(db: Database, candidate: CandidateId, evaluator: EvaluatorId) -> None:
    """Start an evaluation of ``candidate`` under ``evaluator``, cancel it as a person, and end its job."""
    with db.session() as s:
        evaluation, _ = queue.request(s, candidate, evaluator)
        assert queue.start(s, evaluation.id)
        assert queue.cancel(s, candidate, System.HUMAN) == 1
        assert queue.done(s, evaluation.id) is not None
        s.commit()


def current(db: Database, campaign: Campaign) -> EvaluatorId:
    with db.session() as s:
        found = s.get_one(Campaign, campaign.id).evaluator_id
        assert found is not None
        return found


def test_a_cancelled_confirmation_stands_and_no_pass_runs_it_again(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """Its first score beats the best, and a person cancels the evaluation that confirms it: the cancel stands."""
    configure(db, campaign, evaluator=SUBMIT)
    ex = controller(db, runtime, tmp_path)
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\nb\n'))
    evaluator = current(db, campaign)
    with db.session() as s:
        first = s.get_one(Candidate, better)
        record(s, first, evaluator, System.DAEMON, metrics={'score': 3.0, 'size': 9.0}, error=None, seconds=0)
        s.commit()
    assert pool(db, campaign).confirm_due(better)
    cancel_running(db, better, current(db, campaign))
    with db.session() as s:
        assert waiting(s, s.get_one(Campaign, campaign.id)) == [], 'the cancel answers the submission'
    assert not pool(db, campaign).confirm_due(better)
    ex.score_submission(better)
    assert evaluations(db, campaign, better) == (1, 0), 'no confirmation runs, whoever asks to score it'
    assert pool(db, campaign).standing(better).told().startswith('score 3 over 1 eval'), 'it keeps its score'


def test_a_cancelled_confirmation_of_the_baseline_stands_until_someone_asks_again(
    db: Database, campaign: Campaign
) -> None:
    configure(db, campaign, evaluator=SUBMIT)
    base = baseline(db, campaign.id).id
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert [c.id for c in due(s, p)] == [base], 'the baseline has 1 of its 3 evaluations'
    cancel_running(db, base, current(db, campaign))
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert (due(s, p), p.paused) == ([], None), 'it has a score: runs go on, and nothing pauses'
        request_remeasure(s, s.get_one(Candidate, base), System.HUMAN)
        s.commit()
        assert [c.id for c in due(s, p)] == [base], 'the ask lifts the cancel'
        assert s.scalars(select(Cancel)).all() == []


def test_a_cancelled_sealed_evaluation_stands(db: Database, campaign: Campaign, tmp_path: Path) -> None:
    configure(db, campaign, evaluator={'noise_runs': 1, 'sealed': lines('gamed')})
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\n'))
    evaluator = current(db, campaign)
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        first = s.get_one(Candidate, better)
        record(s, first, evaluator, System.DAEMON, metrics={'score': 2.0, 'size': 7.0}, error=None, seconds=0)
        hidden = sealed_evaluator(s, p, p.config)
        assert hidden is not None
        s.commit()
        hidden_id = hidden.id
    assert pool(db, campaign).seal_due(better)
    cancel_running(db, better, hidden_id)
    assert not pool(db, campaign).seal_due(better)
    with db.session() as s:
        assert better not in [c.id for c in due(s, s.get_one(Campaign, campaign.id))]


def test_a_cancelled_remeasure_of_the_carried_best_keeps_it_in_every_pool(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """After an evaluator change the daemon measures the best before it. A cancel of that keeps it the best, and runs
    no longer wait for it."""
    configure(db, campaign, evaluator={'noise_runs': 1})
    ex = controller(db, runtime, tmp_path)
    better = submitted(db, campaign, running(db, campaign), commit(checkout(campaign, tmp_path), 'base\na\n'))
    ex.score_submission(better)
    old = current(db, campaign)
    configure(db, campaign, evaluate=[*EVALUATE[:2], EVALUATE[2] + ' # v2'], evaluator={'noise_runs': 1})
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        current_evaluator(s, p, p.config)
        s.commit()
    cancel_running(db, better, current(db, campaign))
    base = baseline(db, campaign.id).id
    with db.session() as s:
        p = s.get_one(Campaign, campaign.id)
        assert s.get_one(Candidate, better).origin is Origin.RUN
        assert Pool.of(s, p, p.config.all_tracks[0], old).best() == better, "the old evaluator's best stays"
        found = carried(s, p)
        assert found is not None and found.id == better
        assert unmeasured(s, p) == [base], 'runs wait for the baseline alone'
        assert best_of(s, p) == found, 'the best stays where it was'
        assert Pool.of(s, p, p.config.all_tracks[0]).standing(better).told() == 'cancelled: not scored'


def test_a_base_of_another_campaign_is_refused_and_a_campaign_without_a_candidate_has_no_best(
    db: Database, campaign: Campaign
) -> None:
    run_id = running(db, campaign, claimed=False)
    agent = implementer(db, run_id)
    with db.session() as s:
        other = Campaign(name='other', path='/other', config=bare_config('other'))
        s.add(other)
        s.flush()
        stranger = Candidate(campaign_id=other.id, tree_sha='t', revision='r', origin=Origin.BASELINE)
        s.add(stranger)
        s.flush()
        idea = s.scalars(select(Idea).where(Idea.status == IdeaStatus.OPEN)).one()
        with pytest.raises(Refused, match=f'candidate#{stranger.id} is in another campaign than run#{run_id}'):
            backlog_ops.claim(s, idea.id, actor=agent, base=stranger.id)
        empty = Campaign(name='empty', path='/empty', config=bare_config('empty'))
        s.add(empty)
        s.flush()
        assert best_of(s, empty) is None
