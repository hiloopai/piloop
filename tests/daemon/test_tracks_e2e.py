"""A campaign with two tracks, run through the real daemon: claims by title, a best per track, and another track's floor.

The harness is a script that edits files by idea title, so the whole loop runs in CI without a real agent.
"""

import time
from pathlib import Path

from git import Repo
from inline_snapshot import snapshot
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.agents.roles import PILOOP_HINT
from piloop.daemon.main import Daemon
from piloop.db.events import in_order, latest
from piloop.db.models import Campaign, Event
from piloop.ids import TargetRef
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.pool.pool import best_of
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.settings import Settings
from tests.conftest import (
    SETTLE,
    ScriptRuntime,
    factory,
    queued,
    register,
    run_all,
    serving_api,
    workers_of,
    write_script,
)

TOML = """
name = "tracks"
evaluate = ["sh", "-c", "printf '{{\\"metrics\\": {{\\"accuracy\\": %s, \\"cost\\": %s}}}}\\n' $(wc -l < acc.txt) $(wc -l < cost.txt) >> \\"$PILOOP_RESULT_FILE\\""]
evaluate_timeout = 30

[plugins.runtime]
use = "subprocess"

[plugins.harness]
use = ["script"]

[plugins.role.implementer]
timeout = 30
max_concurrent = 2

[[metrics]]
name = "accuracy"
direction = "higher"
role = "primary"
track = "accuracy"
margin = 1
floor = 1

[[metrics]]
name = "cost"
direction = "lower"
role = "primary"
track = "cost"
margin = 1
floor = 5

[evaluator]
scoring = "run_end"
noise_runs = 1

[plugins.harness.script]
model = "test-model"
executable = "{script}"
headless = ["{{prompt}}"]
"""

HARNESS = """#!/bin/sh
title=$({piloop} sql "SELECT 'idea: ' || i.title FROM ideas i JOIN runs r ON r.idea_id = i.id \\
  JOIN agent_sessions a ON a.subject_kind = 'run' AND a.subject_id = r.id WHERE a.id = ${{PILOOP_ACTOR#*#}}")
case "$title" in
  *"idea: trim cost"*) tail -n +2 cost.txt > cost.tmp && mv cost.tmp cost.txt; git commit -qam "trim cost" ;;
  *"idea: slow accuracy"*)
    while [ ! -e {go} ]; do sleep 0.05; done
    echo a >> acc.txt; git commit -qam "slow accuracy" ;;
  *"idea: heavy accuracy"*) echo a >> acc.txt; printf 'c\\nc\\nc\\nc\\n' >> cost.txt; git commit -qam "heavy accuracy" ;;
esac
{piloop} run end submitted
"""
"""Edits by the title of the idea that its run claimed: ``slow accuracy`` waits for the test's go file, so the cost
candidate is scored first. A run that claimed nothing saves nothing. Each ends its run as submitted."""


def test_two_tracks_each_get_a_best_and_one_past_the_other_tracks_floor_is_never_best(tmp_path: Path) -> None:
    path, go = tmp_path / 'tracks', tmp_path / 'go'
    path.mkdir()
    (path / 'acc.txt').write_text('a\n')
    (path / 'cost.txt').write_text('c\nc\nc\n')
    (path / 'brief.md').write_text('# Tracks\n')
    (path / 'piloop.toml').write_text(
        TOML.format(script=write_script(tmp_path / 'harness.sh', HARNESS.format(go=go, piloop=PILOOP_HINT)))
    )
    repo = Repo.init(path, initial_branch='main')
    repo.git.add('-A')
    repo.git.commit('-qm', 'baseline')
    runtime = ScriptRuntime()
    daemon = Daemon(Settings(home=tmp_path / 'home'))
    with serving_api(Settings(home=tmp_path / 'home')):  # each worker's `piloop run end` reaches it
        workers_of(daemon.controller).worker.runtime_factory = factory(runtime)
        campaign = register(daemon.db, path)
        with daemon.db.session() as s:
            ideas = [
                Idea(campaign_id=campaign.id, track=track, title=title, hypothesis=title, created_by='t')
                for title, track in (('slow accuracy', 'accuracy'), ('trim cost', 'cost'))
            ]
            s.add_all(ideas)
            s.commit()
            slow, trim = (i.id for i in ideas)

        daemon.scheduler.reap()
        assert queued(daemon.db, daemon.scheduler, campaign.id, slow, trim) == [1, 2], 'two slots, one idea each'
        assert len(run_all(daemon.scheduler)) == 2
        deadline = time.monotonic() + 120  # a macOS runner is slow
        while not _scored(daemon):
            assert time.monotonic() < deadline, 'the cost candidate was never scored'
            time.sleep(0.05)
        go.touch()
        daemon.scheduler.wait(SETTLE)

        with daemon.db.session() as s:
            p = s.get_one(Campaign, campaign.id)
            rows = s.scalars(select(Candidate).order_by(Candidate.id)).all()
            assert [(c.summary, c.parent_id) for c in rows] == [
                ('baseline', None),
                ('trim cost', 1),
                ('slow accuracy', 1),
            ], 'nothing is merged: each run scores what it made'
            _, trimmed, accurate = rows
            accuracy, cost = p.config.all_tracks
            assert [best_of(s, p, t) for t in (accuracy, cost)] == [accurate, trimmed], 'a best on each track'
            assert [(e.target_id, scored.track, scored.told) for e, scored in in_order(s, CandidateScored)] == (
                snapshot(
                    [
                        (2, 'cost', 'cost 2 over 1 eval: the best of the cost track now, rank 1 of 2; vs parent +1'),
                        (
                            3,
                            'accuracy',
                            'accuracy 2 over 1 eval: the best of the accuracy track now, rank 1 of 3; vs parent +1',
                        ),
                    ]
                )
            )
            assert sorted(s.get_one(Idea, i).track for i in (slow, trim)) == ['accuracy', 'cost']
            heavy_idea = Idea(
                campaign_id=campaign.id, track='accuracy', title='heavy accuracy', hypothesis='h', created_by='t'
            )
            s.add(heavy_idea)
            s.commit()
            heavy_id = heavy_idea.id

        assert len(queued(daemon.db, daemon.scheduler, campaign.id, heavy_id)) == 2, 'two free slots, one idea'
        assert len(run_all(daemon.scheduler)) == 2
        daemon.scheduler.wait(SETTLE)
        with daemon.db.session() as s:
            p = s.get_one(Campaign, campaign.id)
            heavy = s.scalars(select(Candidate).order_by(Candidate.id.desc()).limit(1)).one()
            assert (heavy.summary, heavy.parent_id) == ('heavy accuracy', accurate.id), 'it starts from the best'
            assert told(s, heavy.id) == snapshot(
                'accuracy 3 over 1 eval: it may not be best: floor: cost 7 past its floor 5; vs parent +1, vs best candidate#3 +1'
            ), 'accuracy 3 beats the best, but the cost track holds its floor'
            assert best_of(s, p, accuracy) == s.get_one(Candidate, accurate.id), 'the best stays'
        daemon.shutdown(timeout=5)


def _scored(daemon: Daemon) -> int:
    with daemon.db.session() as s:
        return len(in_order(s, CandidateScored))


def told(s: Session, candidate: CandidateId) -> str | None:
    found = latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate))
    return found[1].told if found is not None else None
