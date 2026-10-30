"""A campaign with two tracks, run through the real daemon: claims by title, rebase on keep, and a floor on another track.

The harness is a script that edits files by idea title, so the whole loop runs in CI without a real agent.
"""

import time
from pathlib import Path

from git import Repo
from sqlalchemy import select

from piloop.agents.roles import PILOOP_HINT
from piloop.daemon.main import Daemon
from piloop.db.events import in_order
from piloop.db.models import Campaign
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import Outcome
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.verdicts.judge import Rebased, head
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

[plugins.role.research]
max_ideas = 0

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
keep lands first. A run that claimed nothing saves nothing. Each ends its run as submitted."""


def test_two_tracks_rebase_a_keep_that_lost_the_race_and_refuse_one_past_the_other_tracks_floor(
    tmp_path: Path,
) -> None:
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
        deadline = time.monotonic() + 120  # four evaluations and a rebase; a macOS runner is slow
        while _verdicts(daemon) != [Outcome.KEEP, Outcome.KEEP]:
            assert time.monotonic() < deadline, 'the cost keep never landed'
            time.sleep(0.05)
        go.touch()
        daemon.scheduler.wait(SETTLE)

        with daemon.db.session() as s:
            rows = s.scalars(select(Candidate).order_by(Candidate.id)).all()
            assert [(c.summary, c.verdict, c.parent_id) for c in rows] == [
                ('baseline', Outcome.KEEP, None),
                ('trim cost', Outcome.KEEP, 1),
                ('slow accuracy', Outcome.INTERESTING, 1),
                ('slow accuracy (rebased onto candidate#2)', Outcome.KEEP, 2),
            ]
            _, trimmed, raced, merged = rows
            assert raced.verdict_reason == (
                'keeps against its own bar, but the head moved on to candidate#2: against candidate#1: accuracy 2 vs 1'
                ' (+1; aggregate: no per-unit results); guardrails: cost 3 vs 3 (+0; aggregate: no per-unit results)'
                ' within its margin 1; rebased onto the head as candidate#4'
            )
            assert merged.verdict_reason == (
                'against candidate#2: accuracy 2 vs 1 (+1; aggregate: no per-unit results); guardrails: cost 2 vs 2'
                ' (+0; aggregate: no per-unit results) within its margin 1'
            ), 'judged on the head, where both tracks gained'
            assert [p.hexsha for p in repo.commit(merged.revision).parents] == [trimmed.revision, raced.revision]
            [(rebased, payload)] = in_order(s, Rebased)
            assert (rebased.target_id, payload) == (raced.id, Rebased(head=trimmed.id, rebased=merged.id))
            assert sorted(s.get_one(Idea, i).track for i in (slow, trim)) == ['accuracy', 'cost']
            trunk = head(s, s.get_one(Campaign, campaign.id))
            assert trunk is not None and trunk.id == merged.id
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
            heavy = s.scalars(select(Candidate).order_by(Candidate.id.desc()).limit(1)).one()
            assert (heavy.summary, heavy.verdict, heavy.verdict_reason, heavy.parent_id) == (
                'heavy accuracy',
                Outcome.DISCARD,
                'floor: cost 6 past its floor 5',
                merged.id,
            ), 'accuracy 3 beats the head, but the cost track holds its floor'
            trunk = head(s, s.get_one(Campaign, campaign.id))
            assert trunk is not None and trunk.id == merged.id, 'the head stays'
        daemon.shutdown(timeout=5)


def _verdicts(daemon: Daemon) -> list[Outcome | None]:
    with daemon.db.session() as s:
        return list(s.scalars(select(Candidate.verdict).where(Candidate.verdict == Outcome.KEEP)))
