"""The paths of ``tests/models/token_trust.tla``, replayed against the real ``Tokens``: two calls that overlap a revoke,
a mint, or a revoke by another process, step by step.

Each call runs ``Tokens.verify`` in a thread of its own, held at its read of the row (``tokens._verified``) and after
it, as the model's ``Start``, ``Read`` and ``Store`` split it. A revoke and a mint run ``Tokens.revoke`` and
``Tokens.mint``, held at their ``forget``. After each step, ``Tokens`` trusts what the model trusts.

The traces cover every transition of two graphs (``token_trust_graph_<graph>.cfg``): a revoke with two calls, one after
the other, and a mint or a revoke by another process with one call. ``scripts/graph_traces.py`` writes them from TLC's
``-dump dot,actionlabels``.
"""

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from piloop.api.contract import Access
from piloop.db.models import Token
from piloop.db.session import Database
from piloop.net import tokens
from piloop.net.tokens import Caller, Tokens

TRACES = Path(__file__).parent / 'traces' / 'token_trust'
NAME = 'gpu-1'
CALLER = Caller(NAME, Access.WORKER)
WAIT = 10
"""How long a step waits for a thread: only a bug makes it wait that long."""

type Itf = bool | str | list[Itf] | dict[str, Itf]


def _value(itf: Itf) -> object:
    match itf:
        case {'#bigint': str(n)}:
            return int(n)
        case {'#set': list(items)}:
            return {_value(i) for i in items}
        case list(items):
            return [_value(i) for i in items]
        case _:
            return itf


def secret(x: int) -> str:
    """The secret of the model's token ``x``: 1 is made first, 2 by the mint."""
    return f'{tokens.PREFIX}model-{x}'


@dataclass
class Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Held:
    """A thread that the replay holds at a point, and lets go of one step at a time."""

    thread: threading.Thread
    there: threading.Event = field(default_factory=threading.Event)
    go: threading.Event = field(default_factory=threading.Event)
    answer: list[Caller | None] = field(default_factory=list)

    def run(self) -> None:
        self.thread.start()
        self.reach()

    def reach(self) -> None:
        """Wait until the thread is held, or has ended."""
        for _ in range(WAIT * 1000):
            if self.there.wait(0.001) or not self.thread.is_alive():
                return
        raise AssertionError('it is neither held nor ended')

    def hold(self) -> None:
        """In the thread: wait here until the replay lets go."""
        self.there.set()
        assert self.go.wait(WAIT), 'never let go of'
        self.go.clear()

    def release(self) -> None:
        self.there.clear()
        self.go.set()
        self.reach()

    def end(self) -> None:
        self.thread.join(WAIT)
        assert not self.thread.is_alive(), 'it did not end'


class Replay:
    """The real ``Tokens`` driven along one path of the model: one object, one token, as at the path's start."""

    def __init__(self, db: Database) -> None:
        self.db, self.clock = db, Clock()
        self.tokens = Tokens(db, trust=1, clock=self.clock)
        self.held: dict[str, Held] = {}
        self.found: dict[int, bool] = {}
        self.names: Iterator[int] = iter((2,))
        """The token that ``tokens.create`` makes next: 2, for the mint."""
        with db.session() as s:
            s.execute(delete(Token))
            s.add(Token(name=NAME, access=Access.WORKER, sha256=tokens._sha256(secret(1))))
            s.commit()
        self.steps: dict[str, Callable[[int, dict[str, object]], None]] = {
            'trusted': self.trusted_answer,
            'start': self.start,
            'read': self.read,
            'store': self.store,
            'revoke_first': lambda _, __: self.write('revoke', lambda: self.tokens.revoke(NAME)),
            'mint_delete': lambda _, __: self.write('mint', lambda: self.tokens.mint(NAME, Access.WORKER)),
            'revoke_second': lambda _, __: self.forget('revoke'),
            'mint_forget': lambda _, __: self.forget('mint'),
            'elsewhere': lambda _, __: self.elsewhere(),
            'tick': lambda _, __: self.tick(),
        }

    def call(self, c: int, state: dict[str, object]) -> Held:
        sec = state['sec']
        assert isinstance(sec, list)
        x = sec[c - 1]
        assert isinstance(x, int)
        name = f'call-{c}'
        verify = lambda: self.held[name].answer.append(self.tokens.verify(secret(x)))  # noqa: E731
        held = self.held[name] = Held(threading.Thread(target=verify, name=name, daemon=True))
        held.run()
        return held

    def trusted_answer(self, c: int, state: dict[str, object]) -> None:
        held = self.call(c, state)
        held.end()
        assert held.answer == [CALLER], 'answered from its trust, with no read'

    def start(self, c: int, state: dict[str, object]) -> None:
        held = self.call(c, state)
        assert held.thread.is_alive() and not held.answer, 'held at its read'

    def read(self, c: int, state: dict[str, object]) -> None:
        found = state['found']
        assert isinstance(found, list)
        self.held[f'call-{c}'].release()
        self.found[c] = found[c - 1]

    def store(self, c: int, _: dict[str, object]) -> None:
        held = self.held[f'call-{c}']
        held.release()
        held.end()
        assert held.answer == [CALLER if self.found[c] else None], 'the caller that it read'

    def write(self, name: str, body: Callable[[], object]) -> None:
        self.held[name] = Held(threading.Thread(target=body, name=name, daemon=True))
        self.held[name].run()
        assert self.held[name].thread.is_alive(), 'held at its forget, after its commit'

    def forget(self, name: str) -> None:
        self.held[name].release()
        self.held[name].end()

    def elsewhere(self) -> None:
        """Another process deletes the token: a session of its own, and no ``forget`` of this object."""
        with self.db.session() as s:
            tokens.revoke(s, NAME)
            s.commit()

    def tick(self) -> None:
        self.clock.now += 1

    def finish(self) -> None:
        """Let every thread that a path left held run to its end: a path may end anywhere in the graph."""
        for held in self.held.values():
            while held.thread.is_alive():
                held.release()

    def trusted(self) -> list[int]:
        """What ``Tokens`` trusts now, as the model writes it: 1 for each secret that it answers without a read."""
        until = {sha256: end for sha256, (end, _) in self.tokens._trusted.items()}
        return [int(until.get(tokens._sha256(secret(x)), 0) > self.clock()) for x in (1, 2)]


@dataclass
class Current:
    """The replay that the held threads belong to."""

    replay: Replay | None = None

    def held(self) -> Held:
        assert self.replay is not None
        return self.replay.held[threading.current_thread().name]


@pytest.fixture
def current(monkeypatch: pytest.MonkeyPatch) -> Current:
    """Hold each call at its read and after it, and each revoke and mint at its forget."""
    now = Current()
    read, forget = tokens._verified, Tokens.forget

    def reading(s: Session, sha256: str) -> Caller | None:
        now.held().hold()
        found = read(s, sha256)
        now.held().hold()
        return found

    def forgetting(self: Tokens, name: str) -> None:
        now.held().hold()
        forget(self, name)

    def token_urlsafe(_: int) -> str:
        assert now.replay is not None
        return secret(next(now.replay.names)).removeprefix(tokens.PREFIX)

    monkeypatch.setattr(tokens, 'secrets', SimpleNamespace(token_urlsafe=token_urlsafe))
    monkeypatch.setattr(tokens, '_verified', reading)
    monkeypatch.setattr(Tokens, 'forget', forgetting)
    return now


@pytest.mark.parametrize('graph', ['revoke', 'env'])
def test_every_transition_of_the_token_trust_model_holds_on_the_real_tokens(
    db: Database, current: Current, graph: str
) -> None:
    """A revoke or a mint through ``Tokens`` that overlaps a call leaves no trust once it returned, and a revoke by
    another process ends within the trust: on every path, ``Tokens`` trusts what the model trusts."""
    paths = sorted(TRACES.joinpath(graph).glob('*.itf.json'))
    assert paths, 'the traces are there'
    for path in paths:
        trace = json.loads(path.read_text())
        current.replay = Replay(db)
        for action, state in zip(trace['#meta']['actions'], trace['states'][1:], strict=True):
            if action == 'Done':  # the end state: no step
                continue
            s = {k: _value(v) for k, v in state['s'].items()}
            op, c = s['op'], s['arg']
            assert isinstance(op, str) and isinstance(c, int)
            current.replay.steps[op](c, s)
            assert current.replay.trusted() == s['trusted'], f'{graph}/{path.name}: after {op}({c})'
        current.replay.finish()
