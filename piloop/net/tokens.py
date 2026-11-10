"""Bearer tokens of ``piloop serve`` and ``piloop cas serve``: ``piloop token create|list|revoke``.

A secret is ``plt_`` and 32 random bytes. It is shown once, when it is made: the ``tokens`` table keeps its SHA-256
only. A token's name is the caller's name, and its access says which routes it may call. Core verifies a token on
every network, before the network identifies a caller that sends none (``piloop.net.server.authorized``).

The daemon also mints a token for each agent that it launches (``mint_agent``), which its worker hands it at its start
as ``PILOOP_AGENT_TOKEN``, as a Kubernetes pod gets its projected service-account token. The agent acts only as itself
with it, and only while its session is live: the token is deleted when the session ends (``end_agent_token``).
"""

import hashlib
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from piloop.actor import Agent, CallerKind
from piloop.api.contract import Access
from piloop.api.views import TokenList, TokenMade, TokenView
from piloop.db.models import LIVE, AgentSession, Token
from piloop.db.session import Database
from piloop.ids import AgentSessionId

PREFIX = 'plt_'
TRUSTED = 4096
"""The most tokens that a ``Tokens`` trusts at once (``trust``): more callers than any home has tokens."""
AGENT_ACCESS = Access.ACT
"""The access of a launched agent's token: every operation on a campaign, and none of the home's own settings."""


@dataclass(frozen=True)
class Caller:
    """Who calls the server: the name of the token's caller, what it may call, and who it is."""

    name: str
    access: Access
    identity: Agent | CallerKind = CallerKind.PERSON
    """The launched agent whose token this is, which acts as that agent and as nobody else, or what the network named:
    a person, who signs as ``human:<name>``, or a service, which signs as ``service:<name>``."""


class NameTaken(ValueError):
    """A token has that name already: revoke it first."""


class NoToken(LookupError):
    """No token has that name."""


def _sha256(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create(s: Session, name: str, access: Access) -> str:
    """Add a token called ``name`` and return its secret, which nothing keeps. Raises ``NameTaken``."""
    return _added(s, Token(name=name, access=access))


def mint_agent(s: Session, agent: Agent) -> SecretStr:
    """Add a token of ``AGENT_ACCESS`` bound to ``agent``'s session, in place of any that it had. The caller commits.

    Raises:
        NameTaken: A token that is no agent's has the agent's signature as its name.
    """
    s.execute(delete(Token).where(Token.agent_session_id == agent.session))
    token = Token(name=agent.signature, access=AGENT_ACCESS, agent_session_id=agent.session)
    return SecretStr(_added(s, token))


def end_agent_token(s: Session, agent_session_id: AgentSessionId) -> None:
    """Delete the token of an agent whose session ended: it acts as nobody from now on. The caller commits.

    So an UPDATE that sets the session live again revives no token.
    """
    s.execute(delete(Token).where(Token.agent_session_id == agent_session_id))


def _added(s: Session, token: Token) -> str:
    secret = PREFIX + secrets.token_urlsafe(32)
    token.sha256 = _sha256(secret)
    s.add(token)
    try:
        s.flush()
    except IntegrityError as e:
        raise NameTaken(f'a token is called {token.name!r} already: `piloop token revoke {token.name}` first') from e
    return secret


def revoke(s: Session, name: str) -> None:
    """Delete the token called ``name``: its next call gets 401. Raises ``NoToken``."""
    if s.execute(delete(Token).where(Token.name == name).returning(Token.id)).first() is None:
        raise NoToken(f'no token is called {name!r}')


def listing(s: Session) -> TokenList:
    """The tokens by name, as the API answers them: never their secrets, which nothing keeps, nor launched agents'."""
    people = select(Token).where(Token.agent_session_id.is_(None)).order_by(Token.name)
    return TokenList(tokens=[_view(t) for t in s.scalars(people)])


def made(s: Session, name: str, access: Access) -> TokenMade:
    """``create``, as the API answers it: with the secret. Raises ``NameTaken``."""
    secret = create(s, name, access)
    token = s.scalars(select(Token).where(Token.name == name)).one()
    return TokenMade(name=token.name, access=token.access, created_at=token.created_at, secret=SecretStr(secret))


def _view(token: Token) -> TokenView:
    return TokenView(name=token.name, access=token.access, created_at=token.created_at)


def verify(s: Session, secret: str) -> Caller | None:
    """The caller that ``secret`` is the token of, or None for an unknown or revoked one, or an agent's that ended."""
    return _verified(s, _sha256(secret))


def _verified(s: Session, sha256: str) -> Caller | None:
    token = s.scalar(select(Token).where(Token.sha256 == sha256))
    match token:
        case None:
            return None
        case Token(agent_session_id=None):
            return Caller(token.name, token.access)
        case _:
            agent = s.get(AgentSession, token.agent_session_id)
            if agent is None or agent.state not in LIVE:
                return None
            return Caller(token.name, token.access, Agent(role=agent.kind, session=agent.id))


@dataclass(frozen=True)
class Tokens:
    """The tokens of a database, which a network provider verifies a caller's secret against.

    A caller that it verified is trusted for ``trust`` seconds: its next calls read no row. A token revoked through this
    object (``revoke``, ``mint``, ``forget``) stops at once. One revoked by another process, or an agent's token whose
    session ended, stops within ``trust``. A secret that verifies as nobody is never trusted: it is read again each
    time. 0 reads every time.
    """

    db: Database
    trust: float = 0
    clock: Callable[[], float] = time.monotonic
    _trusted: dict[str, tuple[float, Caller]] = field(default_factory=dict, repr=False, compare=False)
    """Each trusted caller by the SHA-256 of its secret, with when the trust ends."""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _forgotten: list[int] = field(default_factory=lambda: [0], repr=False, compare=False)
    """How many times ``forget`` ran: a read that one overlaps trusts nothing, as it may have read a revoked token."""

    def verify(self, secret: str) -> Caller | None:
        """The caller that ``secret`` is the token of, or None for an unknown or revoked one."""
        sha256, now = _sha256(secret), self.clock()
        with self._lock:
            trusted, forgotten = self._trusted.get(sha256), self._forgotten[0]
        if trusted is not None and now < trusted[0]:
            return trusted[1]
        with self.db.session() as s:
            caller = _verified(s, sha256)
        with self._lock:
            if caller is None or self.trust <= 0 or self._forgotten[0] != forgotten:
                self._trusted.pop(sha256, None)
                return caller
            if len(self._trusted) >= TRUSTED:  # ended trusts stay until then: the bound is what holds
                self._trusted.clear()
            self._trusted[sha256] = (now + self.trust, caller)
        return caller

    def forget(self, name: str) -> None:
        """Trust no caller of the token called ``name`` any more: it was revoked or replaced."""
        with self._lock:
            self._forgotten[0] += 1
            for sha256 in [k for k, (_, caller) in self._trusted.items() if caller.name == name]:
                del self._trusted[sha256]

    def mint(self, name: str, access: Access) -> str:
        """Add a token called ``name`` in place of the one that has that name, and return its secret."""
        with self.db.session() as s:
            s.execute(delete(Token).where(Token.name == name))
            secret = create(s, name, access)
            s.commit()
        self.forget(name)
        return secret

    def revoke(self, name: str) -> None:
        """Delete the token called ``name``, if there is one: its next call gets 401."""
        with self.db.session() as s:
            s.execute(delete(Token).where(Token.name == name))
            s.commit()
        self.forget(name)
