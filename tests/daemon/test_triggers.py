"""Requested agents launch within ``max_concurrent``, also through the agents' SQL path, and writes are stamped."""

import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from piloop.actor import System
from piloop.config import load_campaign_config
from piloop.daemon.main import ACTOR
from piloop.daemon.scheduler import Scheduler
from piloop.db import sql
from piloop.db.models import AgentSession, AgentSessionState, Campaign
from piloop.db.session import Database
from piloop.ids import AgentSessionId
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.optimization.planner import NO_RUN_ERROR
from piloop.plugins.executor.workers import Workers
from tests.conftest import (
    AUDITOR,
    HARNESS_NOOP,
    SETTLE,
    ScriptRuntime,
    factory,
    run_all,
    serving,
    write_toml,
)

BLOCKING = '#!/bin/sh\nwhile [ ! -f {release} ]; do sleep 0.05; done\n'


pytestmark = pytest.mark.usefixtures('no_fresh_runs')


def scheduler(db: Database, runtime: ScriptRuntime, tmp_path: Path) -> Scheduler:
    controller = serving(RunController(db, Tracer(None), tmp_path, factory(runtime), executor=Workers()))
    return Scheduler(db, controller, Tracer(None), tmp_path, factory(runtime))


def configure(
    db: Database,
    campaign: Campaign,
    harness_body: str = HARNESS_NOOP,
    **roles: dict[str, object],
) -> None:
    """Rewrite the campaign's harness script and role settings (merged over the test toml)."""
    write_toml(Path(campaign.path), harness_body)
    with db.session() as s:
        cfg = load_campaign_config(Path(campaign.path), plugins={'role': roles})
        s.get_one(Campaign, campaign.id).config = cfg
        s.commit()


def agent_sessions(db: Database, kind: str) -> list[tuple[int, AgentSessionState, str | None]]:
    with db.session() as s:
        rows = s.scalars(select(AgentSession).where(AgentSession.kind == kind).order_by(AgentSession.id)).all()
        return [(w.id, w.state, w.reason) for w in rows]


def wait_for(db: Database, agent_session_id: AgentSessionId, state: AgentSessionState) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db.session() as s:
            if s.get_one(AgentSession, agent_session_id).state is state:
                return
        time.sleep(0.02)
    raise AssertionError(f'implementer#{agent_session_id} never reached {state}')


def backdate(db: Database, agent_session_id: AgentSessionId, seconds: float) -> None:
    with db.session() as s:
        w = s.get_one(AgentSession, agent_session_id)
        w.started_at = w.started_at - timedelta(seconds=seconds)
        if w.ended_at is not None:
            w.ended_at = w.ended_at - timedelta(seconds=seconds)
        s.commit()


def test_ideas_updated_at_is_bumped_by_raw_sql_and_orm_updates_alike(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    with db.session() as s:
        idea = Idea(track='score', campaign_id=campaign.id, title='t', hypothesis='h', created_by='human')
        s.add(idea)
        s.commit()
        created = idea.updated_at
        assert idea.updated_by is None, 'no update yet'

    def stamp() -> tuple[datetime, str | None]:
        with db.session() as s:
            row = s.get_one(Idea, idea.id)
            return row.updated_at, row.updated_by

    time.sleep(0.01)
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        sql.run(con, f"UPDATE ideas SET status = 'dropped' WHERE id = {idea.id}")
    raw, by = stamp()
    assert raw > created and by == 'human'

    time.sleep(0.01)
    with db.session() as s:
        same = s.get_one(Idea, idea.id)
        same.priority = 0
        s.commit()
        orm, by = stamp()
        assert orm > raw and by == ACTOR.signature
        assert (same.updated_at, same.updated_by) == (orm, ACTOR.signature), (
            'the instance re-reads what the triggers wrote'
        )

    pinned = created - timedelta(days=1)
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        sql.run(
            con,
            f"UPDATE ideas SET status = 'done', updated_at = '{pinned:%Y-%m-%d %H:%M:%S.%f}' WHERE id = {idea.id}",
        )
    assert stamp() == (pinned, 'human'), 'a statement that sets updated_at itself is left alone'


@pytest.mark.usefixtures('auditor')
def test_manual_requests_are_launched_under_max_concurrent(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    release = tmp_path / 'release'
    configure(db, campaign, BLOCKING.format(release=release))
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        for _ in range(2):
            sql.run(
                con,
                f"INSERT INTO agent_sessions (kind, campaign_id, state) VALUES ('{AUDITOR}', {campaign.id}, 'requested')",
            )
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == [1]
    assert run_all(sched) == []
    wait_for(db, AgentSessionId(1), AgentSessionState.RUNNING)
    assert agent_sessions(db, AUDITOR) == [
        (1, AgentSessionState.RUNNING, None),
        (2, AgentSessionState.REQUESTED, None),
    ]
    release.touch()
    sched.wait(SETTLE)
    assert run_all(sched) == [2]
    sched.wait(SETTLE)
    sched.stop(timeout=5)
    with db.session() as s:
        second = s.get_one(AgentSession, 2)
    assert (second.state, second.harness, second.model) == (AgentSessionState.EXITED, 'script', 'auditor-model')
    assert (
        'Why this agent started (context, not a scope): requested'
        in (tmp_path / 'agent_sessions' / '2' / 'brief.md').read_text()
    )


def test_an_implementer_request_without_a_run_is_refused(
    db: Database, campaign: Campaign, runtime: ScriptRuntime, tmp_path: Path
) -> None:
    with db.session() as s:
        s.add(AgentSession(kind=IMPLEMENTER, campaign_id=campaign.id, reason='by hand'))
        s.commit()
    sched = scheduler(db, runtime, tmp_path)
    assert run_all(sched) == []
    with db.session() as s:
        w = s.get_one(AgentSession, 1)
    assert (w.state, w.error) == (AgentSessionState.KILLED, NO_RUN_ERROR) and w.ended_at is not None
    sched.stop(timeout=1)


@pytest.mark.usefixtures('auditor')
def test_requests_go_through_the_agent_sql_path(db: Database, campaign: Campaign, tmp_path: Path) -> None:
    """The SQL statement that the roles docs give agents passes the authorizer and inserts a request."""
    with closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con:
        result = sql.run(
            con,
            f"INSERT INTO agent_sessions (kind, campaign_id, state) VALUES ('{AUDITOR}', {campaign.id}, 'requested')",
        )
        assert result.last_insert_rowid == 1
        row = sql.run(con, 'SELECT kind, state, harness, reason FROM agent_sessions').rows
    assert row == [(AUDITOR, 'requested', None, None)]
    with (
        closing(sql.connect(tmp_path / 'piloop.db', actor=System.HUMAN)) as con,
        pytest.raises(sqlite3.IntegrityError),
    ):
        sql.run(con, "INSERT INTO agent_sessions (kind, campaign_id, state) VALUES ('janitor', 1, 'requested')")
