"""Research sweeps end to end: a short backlog requests a sweep, which runs on the head and inserts ideas with SQL."""

import re
import shlex
import sys
from pathlib import Path

import pytest
from git import Repo
from sqlalchemy import select

from piloop.agents.roles import PILOOP_HINT as PYTHON_HINT
from piloop.config import load_campaign_config
from piloop.daemon.scheduler import Scheduler
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Comment
from piloop.db.session import Database
from piloop.ids import CommentKind, TargetKind
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.ideas import added_by
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.executor.workers import Workers
from piloop.settings import Settings
from tests.conftest import RESEARCH, SETTLE, ScriptRuntime, factory, run_all, serving, serving_api
from tests.daemon.test_triggers import LOW_WATER, add_ideas, configure, finish_a_run, last_reason, low_water, occupy

pytestmark = pytest.mark.usefixtures('no_fresh_runs')

PYTHON = shlex.quote(sys.executable)
FAKE_RESEARCHER = f"""#!/bin/sh
# a research harness stand-in: proposes through `idea add` (signed by its PILOOP_ACTOR), reports through SQL
sig=$(cat ../brief.md | sed -n "s/.*Sign what you write as '\\([a-z]*#[0-9]*\\)'.*/\\1/p")
campaign=$(cat ../brief.md | sed -n "s/.*for campaign#\\([0-9]*\\).*/\\1/p")
name=$(cat ../brief.md | sed -n "s/.*for campaign#[0-9]* (\\([a-z]*\\)).*/\\1/p")
cat ../brief.md > workspace/brief-seen.txt
git rev-parse HEAD > workspace/head.txt
echo note >> solve.txt
git commit -qam "a sweep may try things"
{PYTHON} -m piloop idea add "$name" 'target encoding' --hypothesis 'categoricals carry signal' --plan 'encode, refit' \\
  --priority 1 --from 'loss table workspace/research/losses.md'
{PYTHON} -m piloop sql "INSERT INTO comments (target_kind, target_id, author, kind, body) VALUES ('campaign', $campaign, '$sig', 'note', 'proposed target encoding; rejected bigger trees (tried in evaluation#1)')"
"""


def test_research_sweep_inserts_ideas_signed_with_its_implementer_id(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, FAKE_RESEARCHER, research={'max_ideas': 2})
    controller = serving(RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers()))
    scheduler = Scheduler(db, controller, Tracer(None), tmp_path, factory(runtime))

    with serving_api(Settings(home=tmp_path)):  # the sweep's `piloop idea add` asks the daemon
        assert run_all(scheduler) == [1], 'a free implementer slot, no idea for it: low water requests a research sweep'
        scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)

    with db.session() as s:
        agent_session = s.get_one(AgentSession, 1)
        assert (agent_session.kind, agent_session.state, agent_session.error) == (
            RESEARCH,
            AgentSessionState.EXITED,
            None,
        )
        [idea] = s.scalars(select(Idea)).all()
        assert (idea.created_by, idea.status, idea.priority, idea.title) == (
            'research#1',
            IdeaStatus.OPEN,
            1,
            'target encoding',
        )
        assert idea.provenance == 'loss table workspace/research/losses.md'
        [note] = s.scalars(select(Comment).where(Comment.target_kind == TargetKind.CAMPAIGN)).all()
        assert (note.target_id, note.author, note.kind) == (campaign.id, 'research#1', CommentKind.NOTE)
    workspace = tmp_path / 'projects' / str(campaign.id) / 'workspace'
    brief = (workspace / 'brief-seen.txt').read_text()
    assert brief.startswith('# Research sweep') and '- Ideas to add: 2 (the board holds 0 of ' in brief
    assert 'Fewer only when what is left duplicates open work; name that work in your note.' in brief
    assert 'seed' not in brief.lower(), 'there are no seeds to develop'
    assert re.search(
        r'^- Measured runs since the last keep: 0, of the \d+ that mark a stall \(two rounds of', brief, re.M
    )
    assert 'stalled, so one of your own ideas pivots' not in brief
    assert (workspace / 'head.txt').read_text().strip() == Repo(campaign.path).head.commit.hexsha, (
        'the sweep ran in a checkout of the baseline, the only candidate'
    )
    assert (
        '- Your cwd is a detached checkout of candidate#1, the head (score=1.0): read and run the code, try'
        ' things. Nothing you commit here is scored or kept; it goes with the checkout when you end.' in brief
    )
    assert (
        not (tmp_path / 'agent_sessions' / '1' / 'worktree').exists()
        and Repo(campaign.path).git.worktree('list').count('\n') == 0
    ), 'the worktree is gone with the sweep'
    with db.session() as s:
        assert s.scalars(select(Candidate)).all() == [s.get_one(Candidate, 1)], "the sweep's commit is not scored"
    assert re.search(r'^- Sweep started at: \d{4}-\d\d-\d\d \d\d:\d\d:\d\d \(UTC', brief, re.MULTILINE), (
        'the sweep needs its start time to query for ideas concurrent sweeps inserted after the history was read'
    )
    tail = brief[brief.index('- Ideas to add: 2') :]
    assert [line.split('|')[1].strip() for line in tail.splitlines() if line.startswith('| #')] == ['#1']
    assert '## Research pack: metrics' in tail and '## History: kept lineage' in tail
    assert tail.rstrip().endswith(
        '## Research pack: orchestrator and human digests and decisions (newest first)\n- None yet.'
    ), 'the sweep reads the research pack from its brief instead of querying for it'
    assert '## History: ideas (0 of 0)' in tail and '## History: kept lineage' in tail
    assert brief == (tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text()
    assert (tmp_path / 'agent_sessions' / '1' / 'transcript.txt').is_file()


FAKE_PIVOT = f"""#!/bin/sh
# a research harness stand-in on a stalled sweep: adds the pivot, and drops an open idea it duplicates
cat ../brief.md > workspace/brief-seen.txt
{PYTHON} -m piloop idea add demo 'deeper trees' --hypothesis 'a deeper tree fits the interactions' --from 'l.md'
{PYTHON} -m piloop sql "UPDATE ideas SET status = 'dropped' WHERE id = 1"
"""


def test_a_stalled_sweep_adds_its_pivot_however_full_the_backlog(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    """The backlog is full but the campaign has stalled, so the brief asks for one idea of research's own, the pivot.

    The sweep adds it, open on the board, and counts as one that added an idea: no back-off.
    """
    configure(
        db,
        campaign,
        FAKE_PIVOT,
        implementer={'max_concurrent': 1},
        research={'triggers': [*LOW_WATER, 'stalled'], 'max_ideas': 2},
        trigger=low_water(0),
    )
    occupy(db, campaign)
    add_ideas(db, campaign, 1)
    finish_a_run(db, campaign, 'tried once')
    finish_a_run(db, campaign, 'tried twice')
    controller = serving(RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers()))
    scheduler = Scheduler(db, controller, Tracer(None), tmp_path, factory(runtime))

    with serving_api(Settings(home=tmp_path)):  # the sweep's `piloop idea add` asks the daemon
        assert run_all(scheduler) == [1], 'the one slot is busy and the backlog is at its target, but it stalled'
        assert last_reason(db) == 'stalled, step 1'
        scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)

    brief = (tmp_path / 'agent_sessions' / '1' / 'brief.md').read_text()
    assert '- Ideas to add: 1 (the board holds 1 of 1 runnable ideas' in brief
    assert (
        '- Measured runs since the last keep: 2, of the 2 that mark a stall (two rounds of [plugins.role.implementer]'
        ' max_concurrent = 1): stalled, so one of your own ideas pivots (section 3).' in brief
    )
    assert f'{PYTHON_HINT} idea add demo' in brief
    with db.session() as s:
        pivot = s.scalars(select(Idea).where(Idea.title == 'deeper trees')).one()
        assert (pivot.status, pivot.created_by) == (IdeaStatus.OPEN, 'research#1')
        assert s.get_one(Idea, 1).status is IdeaStatus.DROPPED
        assert added_by(s, campaign.id) >= {'research#1'}, 'the sweep added an idea: no back-off'


def test_with_several_tracks_research_reads_them_and_names_the_track_of_each_idea(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    configure(db, campaign, '#!/bin/sh\ncat ../brief.md > workspace/brief-seen.txt\n', research={'max_ideas': 1})
    with db.session() as s:
        tracks = {
            'metrics': [
                {'name': 'score', 'direction': 'higher', 'role': 'primary', 'track': 'accuracy', 'margin': 1},
                {'name': 'size', 'direction': 'lower', 'role': 'primary', 'track': 'cost', 'margin': 5},
            ],
            'plugins': {'role': {'research': {'max_ideas': 1, 'timeout': 20}, 'implementer': {'max_concurrent': 2}}},
        }
        s.get_one(Campaign, campaign.id).config = load_campaign_config(Path(campaign.path), **tracks)
        s.commit()
    controller = serving(RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers()))
    scheduler = Scheduler(db, controller, Tracer(None), tmp_path, factory(runtime))
    assert run_all(scheduler) == [1]
    scheduler.wait(SETTLE)
    scheduler.stop(timeout=5)
    brief = (tmp_path / 'projects' / str(campaign.id) / 'workspace' / 'brief-seen.txt').read_text()
    assert '- Your cwd is a detached checkout of candidate#1, the head (score=1.0, size=5.0)' in brief
    assert '- Give every idea the track it serves (`--track`).' in brief
    assert '- Tracks: accuracy (primary score, higher is better); cost (primary size, lower' in brief
    assert '## Tracks\n\n- accuracy: score 1 at the head (+0 on the start); 0 trunk keeps;' in brief
