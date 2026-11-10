"""Bearer tokens: a secret is shown once and only its hash is kept; a revoked one is unknown; agents cannot add one."""

import hashlib
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from piloop.actor import Agent, System
from piloop.agents.results import finish
from piloop.api.contract import Access
from piloop.db import sql
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Token
from piloop.db.session import Database
from piloop.net import tokens
from piloop.net.tokens import Caller, Tokens


def test_a_token_is_kept_as_its_hash_and_verifies_to_its_caller(db: Database) -> None:
    with db.session() as s:
        secret = tokens.create(s, 'gpu1', Access.WORKER)
        s.commit()
        token = s.scalars(select(Token)).one()
    assert secret.startswith('plt_') and len(secret) > 40
    assert secret not in (token.name, token.sha256)
    assert token.sha256 == hashlib.sha256(secret.encode()).hexdigest()
    assert Tokens(db).verify(secret) == Caller('gpu1', Access.WORKER)
    assert Tokens(db).verify(secret + 'x') is None


def test_a_name_has_one_token_and_a_revoked_one_verifies_no_more(db: Database) -> None:
    with db.session() as s:
        tokens.create(s, 'gpu1', Access.WORKER)
        with pytest.raises(tokens.NameTaken, match='piloop token revoke gpu1'):
            tokens.create(s, 'gpu1', Access.WORKER)
        s.rollback()
        secret = tokens.create(s, 'gpu1', Access.WORKER)
        s.commit()
        tokens.revoke(s, 'gpu1')
        s.commit()
        with pytest.raises(tokens.NoToken):
            tokens.revoke(s, 'gpu1')
    assert Tokens(db).verify(secret) is None


def test_piloop_sql_reads_the_tokens_but_cannot_write_one(db: Database, tmp_path: Path) -> None:
    with closing(sql.connect(tmp_path / 'piloop.db', actor=Agent(role='research', session=4))) as con:
        assert tuple(con.execute('SELECT count(*) FROM tokens').fetchone()) == (0,)
        with pytest.raises(sqlite3.DatabaseError, match='not authorized'):
            con.execute("INSERT INTO tokens (name, access, sha256) VALUES ('agent', 'worker', 'x')")


def test_mint_replaces_the_token_of_its_name_and_revoke_of_a_gone_one_is_a_success(db: Database) -> None:
    first = Tokens(db).mint('gpu-1', Access.WORKER)
    second = Tokens(db).mint('gpu-1', Access.WORKER)
    assert (Tokens(db).verify(first), Tokens(db).verify(second)) == (None, Caller('gpu-1', Access.WORKER))
    Tokens(db).revoke('gpu-1')
    Tokens(db).revoke('gpu-1')
    assert Tokens(db).verify(second) is None


def test_a_deleted_agent_session_s_id_is_never_used_again_so_its_token_never_acts_as_a_later_agent(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """Delete the newest session (by ``piloop sql``, or with its campaign) and the next one gets a new id. Before,
    ``agent_sessions`` had no AUTOINCREMENT: the next session took the id, and the old token acted as the new agent."""
    with db.session() as s:
        old = AgentSession(kind='implementer', campaign_id=campaign.id, state=AgentSessionState.RUNNING)
        s.add(old)
        s.flush()
        secret = tokens.mint_agent(s, Agent(role=old.kind, session=old.id)).get_secret_value()
        s.commit()
        old_id = old.id
    with closing(sql.connect(tmp_path / 'piloop.db', actor=Agent(role='implementer', session=old_id))) as con:
        con.execute('DELETE FROM agent_sessions WHERE id = ?', (old_id,))
    with db.session() as s:
        new = AgentSession(kind='implementer', campaign_id=campaign.id, state=AgentSessionState.STARTING)
        s.add(new)
        s.commit()
        assert new.id > old_id
        assert tokens.verify(s, secret) is None


def test_an_agent_s_token_ends_with_its_session_so_an_update_that_sets_it_live_again_revives_nothing(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """The close of the session deletes its token. Before, the token stayed and acted again once a raw UPDATE set the
    session live."""
    with db.session() as s:
        w = AgentSession(kind='implementer', campaign_id=campaign.id, state=AgentSessionState.RUNNING)
        s.add(w)
        s.flush()
        secret = tokens.mint_agent(s, Agent(role=w.kind, session=w.id)).get_secret_value()
        finish(s, w, AgentSessionState.EXITED, None)
        s.commit()
        assert s.scalars(select(Token)).all() == []
    with closing(sql.connect(tmp_path / 'piloop.db', actor=Agent(role='implementer', session=w.id))) as con:
        con.execute("UPDATE agent_sessions SET state = 'running', ended_at = NULL WHERE id = ?", (w.id,))
    assert Tokens(db).verify(secret) is None


class Clock:
    """A monotonic clock that the test moves."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def reads(db: Database, call: Callable[[], object]) -> int:
    """How many SQL statements ``call`` runs on ``db``."""
    statements: list[str] = []

    def counted(_conn: object, _cursor: object, statement: str, *_: object) -> None:
        statements.append(statement)

    event.listen(db.engine, 'before_cursor_execute', counted)
    try:
        call()
    finally:
        event.remove(db.engine, 'before_cursor_execute', counted)
    return len(statements)


def test_a_verified_token_is_trusted_for_its_time_and_a_revoke_through_this_object_ends_it_at_once(
    db: Database,
) -> None:
    clock = Clock()
    trusted = Tokens(db, trust=10, clock=clock)
    secret = trusted.mint('gpu-1', Access.WORKER)
    assert reads(db, lambda: trusted.verify(secret)) > 0, 'the first call reads its row'
    assert reads(db, lambda: trusted.verify(secret)) == 0, 'the next ones, within the trust, read nothing'
    clock.now += 10
    assert reads(db, lambda: trusted.verify(secret)) > 0, 'its trust ended: read again'
    trusted.revoke('gpu-1')
    assert trusted.verify(secret) is None, 'revoked through this object: at once'
    again = trusted.mint('gpu-1', Access.WORKER)
    assert trusted.verify(again) == Caller('gpu-1', Access.WORKER)
    trusted.mint('gpu-1', Access.WORKER)
    assert trusted.verify(again) is None, 'replaced through this object: at once'


def test_a_token_revoked_by_another_process_stops_within_the_trust(db: Database, tmp_path: Path) -> None:
    clock = Clock()
    trusted = Tokens(db, trust=10, clock=clock)
    secret = trusted.mint('gpu-1', Access.WORKER)
    assert trusted.verify(secret) == Caller('gpu-1', Access.WORKER)
    other = Database(str(db.engine.url), actor=System.HUMAN)
    with other.session() as s:  # as `piloop token revoke` on the database does
        tokens.revoke(s, 'gpu-1')
        s.commit()
    other.close()
    clock.now += 9.9
    assert trusted.verify(secret) == Caller('gpu-1', Access.WORKER), 'trusted until its time ends'
    clock.now += 0.1
    assert trusted.verify(secret) is None


def test_a_secret_that_verifies_as_nobody_is_never_trusted_and_the_trusted_are_bounded(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted = Tokens(db, trust=10, clock=Clock())
    assert trusted.verify('plt_unknown') is None
    with db.session() as s:
        s.add(Token(name='late', access=Access.WORKER, sha256=hashlib.sha256(b'plt_unknown').hexdigest()))
        s.commit()
    assert trusted.verify('plt_unknown') == Caller('late', Access.WORKER), 'its refusal was not kept'
    monkeypatch.setattr(tokens, 'TRUSTED', 1)
    second = trusted.mint('gpu-2', Access.WORKER)
    assert trusted.verify(second) == Caller('gpu-2', Access.WORKER)
    assert len(trusted._trusted) == 1, 'one at most: the full map was cleared for it'


def test_a_revoke_while_a_call_reads_the_token_leaves_it_untrusted(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The call read the token before the revoke: what it read is not trusted, so the next call reads again."""
    trusted = Tokens(db, trust=10, clock=Clock())
    secret = trusted.mint('gpu-1', Access.WORKER)
    verified = tokens._verified

    def revoked_meanwhile(s: Session, sha256: str) -> Caller | None:
        found = verified(s, sha256)
        elsewhere = threading.Thread(target=trusted.revoke, args=('gpu-1',))  # another request's thread
        elsewhere.start()
        elsewhere.join()
        return found

    monkeypatch.setattr(tokens, '_verified', revoked_meanwhile)
    assert trusted.verify(secret) == Caller('gpu-1', Access.WORKER), 'it read the token before the revoke'
    monkeypatch.setattr(tokens, '_verified', verified)
    assert trusted.verify(secret) is None
