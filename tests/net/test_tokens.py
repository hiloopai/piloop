"""Bearer tokens: a secret is shown once and only its hash is kept; a revoked one is unknown; agents cannot add one."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import select

from piloop.actor import Agent
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
