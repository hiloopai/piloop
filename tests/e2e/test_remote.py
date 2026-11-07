"""A small campaign on a piloop server that runs elsewhere, driven only through its API and the CLI, as its users drive it.

    PILOOP_HTTPS_TOKEN=<token> uv run pytest -m e2e tests/e2e/test_remote.py -rA --e2e-server https://piloop.example.com \
        [--e2e-secret ANTHROPIC_API_KEY] [--e2e-campaign <name>] [--e2e-harness claude] [--e2e-model <model>]

The server is any ``piloop serve --listen`` that this machine reaches with a bearer token in ``PILOOP_HTTPS_TOKEN``, of
``act`` access, that signs as a person: only a person stops a campaign by hand, and only a person's message (or an
escalation) wakes the orchestrator. A service account's token fails the first test. A person registers a toy repository
by its URL, adds an idea and writes to the orchestrator. piloop launches the orchestrator on the server's workers within
``WAKE_WAIT``, and it answers the message. The tests also act as an orchestrator from the CLI (``PILOOP_SERVER``): they
add an idea, pause and resume the campaign, and fork it, and its stop is refused. The person stops, reopens and retires
it, and no stop wakes the orchestrator. Two implementers run on the server's workers: each claims an idea or gives up,
and a submission is scored.

The toy is published as a secret gist (``gh``, logged in with the ``gist`` scope), which the server clones: the server
needs to reach github.com. Its ``piloop.toml`` gives the harness each ``--e2e-secret`` from the server's secret of that
name. Its campaign has the same name on each run (``--e2e-campaign``, default ``e2e-remote``): the scenario retires a
campaign of that name that an earlier run left, and retires its own at the end. The gist is deleted at the end. About
3 minutes and 0.2 USD on ``claude-haiku-4-5``, mostly cache reads.
"""

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

import pytest
import tomli_w
from pydantic import JsonValue

from piloop.actor import ORCHESTRATING, PERSON, System, by_a_person
from piloop.api.client import Client, Refused
from piloop.api.contract import (
    MAX_LIMIT,
    MAX_POLL_SECONDS,
    AckBody,
    CancelBody,
    Clone,
    EventsQuery,
    MessageBody,
    PauseBody,
    Problem,
    Remote,
    RetireBody,
    Selection,
    StopBody,
)
from piloop.api.views import AckOutcome, CampaignState, Envelope
from piloop.ids import AgentSessionId, CampaignId, Ending, RoleName
from piloop.plugins.campaign_type.kit.backlog.wire import IdeaBody
from piloop.plugins.network.https import TOKEN_ENV, Https
from tests.conftest import EveryClient
from tests.e2e.campaign import CHEAPEST_MODEL, CommandFailed, needs
from tests.e2e.conftest import Install
from tests.isolation import CALLER_ENV

pytestmark = pytest.mark.e2e

ROLE_TIMEOUT = 300
MAX_CONCURRENT = 2
"""The implementers that run at once: one run on each free slot."""
PLUGINS: dict[str, object] = {
    'role': {
        'implementer': {'timeout': ROLE_TIMEOUT, 'max_concurrent': MAX_CONCURRENT},
        'orchestrator': {
            'triggers': ['news'],
            'addendum': 'orchestrator.md',
            'timeout': ROLE_TIMEOUT,
            'quiet_seconds': 5,
        },
    },
    'stop_rule': {'max_runs': 3, 'max_tokens': 4_000_000, 'hold_at': {'tokens': 3_000_000}},
}
CONFIG = {
    'evaluate': ['python3', 'evaluate.py'],
    'evaluate_timeout': 120,
    'plugins': PLUGINS,
    'metrics': [{'name': 'bytes', 'direction': 'lower', 'role': 'primary'}],
}
"""The toy's ``piloop.toml``, without its name and harness: an orchestrator that piloop launches on a person's message or
an escalation, and limits only as a backstop on what it costs. Two ideas run, so ``max_runs`` stops it only when both
runs gave up."""
ORCHESTRATOR = """# This campaign is a test

Answer each message and escalation, and acknowledge it. Change nothing else: add, develop or change no idea, exclude no
candidate, and launch, pause or stop nothing. The tests do that themselves.
"""
BRIEF = """# Make primes.py smaller

`primes.py` prints the first 50 prime numbers, one per line. Make the file as small as you can, in bytes, while it prints
exactly the same output. Edit only `primes.py`. `python3 evaluate.py` checks the output and prints the size. Keep it plain
Python 3 with no imports.
"""
EVALUATE = """import json, os, pathlib, subprocess, sys

def primes(n):
    out, k = [], 2
    while len(out) < n:
        if all(k % d for d in range(2, int(k ** 0.5) + 1)):
            out.append(k)
        k += 1
    return out

run = subprocess.run([sys.executable, 'primes.py'], capture_output=True, text=True, timeout=60)
if run.returncode != 0 or [int(line) for line in run.stdout.split()] != primes(50):
    sys.exit('primes.py does not print the first 50 primes')
line = json.dumps({'metrics': {'bytes': len(pathlib.Path('primes.py').read_bytes())}})
if path := os.environ.get('PILOOP_RESULT_FILE'):
    with open(path, 'a') as result:
        result.write(line + '\\n')
else:
    print(line)
"""
PRIMES = """# Prints the first 50 prime numbers, one per line.
def is_prime(candidate_number):
    if candidate_number < 2:
        return False
    divisor = 2
    while divisor < candidate_number:
        if candidate_number % divisor == 0:
            return False
        divisor = divisor + 1
    return True


found_primes = []
current_number = 2
while len(found_primes) < 50:
    if is_prime(current_number):
        found_primes.append(current_number)
    current_number = current_number + 1

for prime in found_primes:
    print(prime)
"""
WAIT = 25.0
"""Seconds that the CLI's ``inbox wait`` holds a request."""
BASELINE_WAIT = 900
"""Seconds to wait for the baseline: a provisioned evaluation worker can take minutes to start."""
RUNS_WAIT = 3 * ROLE_TIMEOUT + 600
"""Seconds to wait for both runs to end and be scored: their workers start, they work, then they are measured."""
RETIRE_WAIT = ROLE_TIMEOUT + 300
"""Seconds to wait for a stop to be over before a campaign retires: its runs end."""
WAKE_WAIT = 120
"""Seconds from a person's message to the orchestrator that it wakes: the orchestrator's ``quiet_seconds``, then a pass."""
QUIET = 90
"""Seconds that the feed is read after a stop, to show that the stop woke no orchestrator."""


@dataclass(frozen=True)
class Claim:
    """A run's implementer, and the idea of the board that it claimed or proposed: None when it gave up first."""

    agent: int
    idea: int | None
    proposed: bool


@dataclass
class Server:
    """The campaign on the server, and what the tests learn of it as they go."""

    url: str
    name: str
    repo: str
    """The URL that the server clones."""
    api: EveryClient
    """The person's client of the API."""
    env: dict[str, str] = field(repr=False)
    """The environment of the CLI, as the orchestrator. Not in the repr: it holds the token."""
    id: CampaignId | None = None
    """The campaign's id, once it registered: its name may name another campaign once it is retired."""
    again: CampaignId | None = None
    """The id of the campaign that registers the URL again, under the same name."""
    forked: CampaignId | None = None
    """The id of the campaign that forks this one."""
    cursor: int = 0
    """The feed is read after it."""
    events: list[Envelope] = field(default_factory=list)
    baseline: int = 0
    ideas: dict[str, int] = field(default_factory=dict)
    """The id of each idea that the tests made, by key."""
    runs: dict[int, Claim] = field(default_factory=dict)
    """Each run that a free slot queued, by id: its implementer and the idea it claimed or proposed."""

    def piloop(self, *args: str) -> str:
        """Run the CLI against the server as the orchestrator, and return its stdout."""
        proc = subprocess.run(['piloop', *args], env=self.env, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            raise CommandFailed(
                f'`piloop {" ".join(args)}` exited {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}'
            )
        return proc.stdout

    def state(self) -> CampaignState:
        """The campaign as ``GET /v1/status`` shows it."""
        [found] = self.api.status(Selection(campaign=str(self.id))).campaigns
        assert isinstance(found, CampaignState), found
        return found

    def seen(self, match: Callable[[Envelope], bool], what: str, timeout: float) -> Envelope:
        """The first event of the campaign that ``match`` takes, read from the feed with long-polls until ``timeout``."""
        deadline = time.monotonic() + timeout
        read = 0
        while True:
            if found := next((e for e in self.events[read:] if match(e)), None):
                return found
            read = len(self.events)
            left = deadline - time.monotonic()
            assert left > 0, f'timed out after {timeout:.0f}s waiting for {what}; last events: {self.tail()}'
            query = EventsQuery(
                campaign=str(self.id), after=self.cursor, limit=MAX_LIMIT, wait=min(left, MAX_POLL_SECONDS)
            )
            batch = self.api.events(query)
            self.events += batch.events
            self.cursor = batch.next

    def none(self, match: Callable[[Envelope], bool], what: str, seconds: float) -> None:
        """Read the feed for ``seconds`` with long-polls, and fail on the first new event that ``match`` takes."""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            query = EventsQuery(
                campaign=self.name, after=self.cursor, limit=MAX_LIMIT, wait=min(left, MAX_POLL_SECONDS)
            )
            batch = self.api.events(query)
            self.events += batch.events
            self.cursor = batch.next
            found = next((e for e in batch.events if match(e)), None)
            assert found is None, f'{what}: {found}'

    def of(self, kind: str) -> list[Envelope]:
        """The events of ``kind`` read so far."""
        return [e for e in self.events if e.kind == kind]

    def of_role(self, kind: str, role: str) -> list[Envelope]:
        """The events of ``kind`` read so far whose payload names ``role``."""
        return [e for e in self.of(kind) if payload(e).get('role') == role]

    def tail(self) -> list[str]:
        return [f'{e.id} {e.kind} {e.subject.kind}#{e.subject.id} by {e.actor}' for e in self.events[-8:]]


def payload(event: Envelope) -> dict[str, JsonValue]:
    assert isinstance(event.payload, dict), event
    return event.payload


def kind_on(kind: str, subject: str, subject_id: int) -> Callable[[Envelope], bool]:
    return lambda e: e.kind == kind and e.subject.kind == subject and e.subject.id == subject_id


def added_event(api: Client, campaign: CampaignId) -> Envelope:
    """The ``campaign.added`` of ``campaign``, read page by page from the start of the feed.

    A campaign filter also keeps every event whose row is gone. On a server with a history, those can fill the first
    page, so one read of the campaign's events can miss its own ``campaign.added``.
    """
    is_it, after = kind_on('campaign.added', 'campaign', campaign), 0
    while True:
        query = EventsQuery(campaign=str(campaign), kind=('campaign.added',), after=after, limit=MAX_LIMIT)
        batch = api.events(query)
        if found := next((e for e in batch.events if is_it(e)), None):
            return found
        assert batch.more, f'the feed has no campaign.added of campaign {campaign}'
        after = batch.next


def publish(directory: Path, files: dict[str, str]) -> str:
    """Write ``files`` in ``directory``, publish them as a secret gist, and return its URL. ``gh`` runs in the caller's
    environment, where it is logged in."""
    if shutil.which('gh') is None:
        raise RuntimeError('the toy is published as a gist: install gh and run `gh auth login`')
    for name, text in files.items():
        (directory / name).write_text(text)
    out = subprocess.run(
        ['gh', 'gist', 'create', '--desc', f'piloop e2e toy {directory.name}', *files],
        cwd=directory,
        env=CALLER_ENV,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.fixture(scope='module')
def server(request: pytest.FixtureRequest) -> Iterator[Server]:
    """The toy's campaign on the server of ``--e2e-server``, as ``--e2e-campaign``. It installs piloop only then."""
    url = request.config.getoption('e2e_server')
    if url is None:
        pytest.skip('pass --e2e-server <url> to run the scenario against a server')
    if not (token := CALLER_ENV.get(TOKEN_ENV)):
        raise pytest.UsageError(f'set {TOKEN_ENV} to a token of the server with act access')
    piloop_install: Install = request.getfixturevalue('piloop_install')
    harness = request.config.getoption('e2e_harness')
    model = request.config.getoption('e2e_model') or CHEAPEST_MODEL[harness]
    secrets = request.config.getoption('e2e_secret') or []
    name = request.config.getoption('e2e_campaign') or 'e2e-remote'
    directory = piloop_install.root / name
    directory.mkdir()
    harnesses = {'use': [harness], harness: {'model': model, 'env': {s: {'secret': s} for s in secrets}}}
    toml = CONFIG | {
        'name': name,
        'plugins': {**PLUGINS, 'harness': harnesses},
    }
    files = {
        'piloop.toml': tomli_w.dumps(toml),
        'brief.md': BRIEF,
        'evaluate.py': EVALUATE,
        'primes.py': PRIMES,
        'orchestrator.md': ORCHESTRATOR,
        '.gitignore': 'workspace\n__pycache__\n',
    }
    gist = publish(directory, files)
    with mock.patch.dict(os.environ, {TOKEN_ENV: token}):
        http = Https().client(url)
    env = piloop_install.env(directory / 'home') | {
        'PILOOP_SERVER': url,
        TOKEN_ENV: token,
        'PILOOP_ACTOR': System.ORCHESTRATOR.signature,
    }
    try:
        with EveryClient.connect(http) as api:
            for left in (c for c in api.status(Selection()).campaigns if c.name == name):
                retire(api, str(left.id), 'e2e: an earlier run left it')
            found = Server(url, name, f'{gist}.git', api, env)
            yield found
            for campaign in (found.id, found.forked, found.again):
                if campaign and api.status(Selection(campaign=str(campaign))).campaigns[0].status != 'retired':
                    retire(api, str(campaign), 'e2e: the scenario ended')
    finally:
        subprocess.run(['gh', 'gist', 'delete', gist.rpartition('/')[2], '--yes'], env=CALLER_ENV, check=True)


def retire(api: Client, ref: str, reason: str) -> CampaignState:
    """Stop the campaign that ``ref`` names unless it is stopped, and retire it once its stop is over.

    An orchestrator that news woke before the stop and that starts meanwhile is cancelled: nobody reads its answer, and
    the retire would wait for it.

    Raises:
        Refused: ``ref`` names no campaign, or the stop is not over within ``RETIRE_WAIT``.
    """
    found = api.status(Selection(campaign=ref))
    [state] = found.campaigns
    campaign, cursor, deadline = str(state.id), found.cursor, time.monotonic() + RETIRE_WAIT
    pending: list[str] = []
    if state.status == 'active':
        api.stop(campaign, StopBody(reason=reason, ending=Ending.NOW))
        pending.append('is active')  # until the daemon acts on the stop that it was asked for
    while True:
        try:
            retired = api.retire(campaign, RetireBody(reason=reason)).campaign
        except Refused as e:
            waits = e.error == Problem.STILL_STOPPING or any(p in str(e) for p in pending)
            if not waits or time.monotonic() > deadline:
                raise
            batch = api.events(EventsQuery(campaign=campaign, after=cursor, wait=MAX_POLL_SECONDS))
            cursor = batch.next
            for woken in batch.events:
                if woken.kind == 'agent_session.requested' and payload(woken)['role'] == 'orchestrator':
                    cancel(api, AgentSessionId(woken.subject.id))
            continue
        assert isinstance(retired, CampaignState), retired
        return retired


def cancel(api: Client, agent: AgentSessionId) -> None:
    """Cancel ``agent`` at once, unless it ended already."""
    try:
        api.cancel_agent(agent, CancelBody(ending=Ending.NOW))
    except Refused as e:
        if 'is not requested, starting or running' not in str(e):  # it ended before the cancel: nothing to cancel
            raise


def test_registers_a_repository_by_its_url(server: Server) -> None:
    """``POST /v1/campaigns`` with ``{clone: {url}, name}``: the server clones the toy and adds its campaign."""
    registered = server.api.register_campaign(Clone(clone=Remote(url=server.repo), name=server.name))
    assert isinstance(registered.campaign, CampaignState), registered.campaign
    assert registered.campaign.name == server.name
    assert registered.campaign.status == 'active'
    server.id, server.cursor, server.baseline = registered.campaign.id, registered.cursor, registered.baseline
    added = added_event(server.api, server.id)
    if not by_a_person(added.actor):
        server.api.pause(str(server.id), PauseBody(reason='e2e: the token signs as no person'))
        pytest.fail(f'the token signs as {added.actor}: the scenario stops campaigns, which only a person does')
    print(f'{server.name} from {server.repo} by {added.actor}: baseline candidate#{server.baseline}')


@needs(test_registers_a_repository_by_its_url)
def test_measures_the_baseline(server: Server) -> None:
    def baseline(e: Envelope) -> bool:
        return e.kind == 'measurement.recorded' and payload(e)['candidate'] == server.baseline

    measured = payload(server.seen(baseline, 'the measurement of the baseline', BASELINE_WAIT))
    assert measured['error'] is None, measured
    assert measured['metrics'] == {'bytes': len(PRIMES.encode())}, measured
    print(f'baseline: {measured["metrics"]}')


@needs(test_measures_the_baseline)
def test_the_orchestrator_adds_an_idea(server: Server) -> None:
    title = 'Strip the comments and the long names from primes.py'
    plan = (
        'In primes.py, delete the comment, rename every variable to one letter, and remove the blank lines. Keep the'
        ' same algorithm. Run `python3 evaluate.py`, then commit. Change nothing else.'
    )
    server.piloop('idea', 'add', server.name, title, '--plan', plan, '--priority', '1')
    created = server.seen(lambda e: e.kind == 'idea.created' and e.actor.startswith(ORCHESTRATING), 'idea.created', 60)
    server.ideas['added'] = created.subject.id
    print(f'idea#{created.subject.id} by {created.actor}')


@needs(test_measures_the_baseline)
def test_a_person_adds_an_idea_that_is_open_on_the_board(server: Server) -> None:
    """The person's idea is open at once: a worker can claim it, with no one to develop it first."""
    title = 'A sieve in a list comprehension is shorter than the trial division loop'
    plan = (
        'Rewrite primes.py as a short comprehension that prints the first 50 primes. Run `python3 evaluate.py`,'
        ' then commit. Change nothing else.'
    )
    body = IdeaBody(title=title, plan=plan, provenance=f'candidate#{server.baseline}', priority=1)
    added = server.api.add_idea(server.name, body)
    assert added.idea.status == 'open', added.idea
    assert added.idea.created_by.startswith(PERSON), added.idea
    server.ideas['person'] = added.idea.id
    print(f'idea#{added.idea.id} by {added.idea.created_by}')


@needs(test_measures_the_baseline)
def test_a_message_wakes_the_orchestrator_that_piloop_launches_and_it_answers(server: Server) -> None:
    """``POST /v1/campaigns/{campaign}/messages`` by a person wakes the orchestrator within ``WAKE_WAIT``, on a worker, and
    its reply reaches the person.

    Nothing else woke it before: an agent's idea is news, and news wakes no one. Each acknowledges what the other wrote:
    the author of a comment cannot.
    """
    assert server.state().roles[RoleName('orchestrator')].triggered
    assert not server.of_role('agent_session.requested', 'orchestrator'), 'only a person or an escalation wakes it'
    sent = server.api.message(
        server.name, MessageBody(body='e2e: how many bytes is primes.py now? Reply with the number.')
    )
    started = time.monotonic()
    woken = server.seen(
        lambda e: e.kind == 'agent_session.requested' and payload(e)['role'] == 'orchestrator',
        'the orchestrator that the message wakes',
        WAKE_WAIT,
    )
    print(f'orchestrator#{woken.subject.id} requested {time.monotonic() - started:.0f}s after the message')

    def answered(e: Envelope) -> bool:
        return e.kind == 'comment.decision' and e.actor.startswith('orchestrator#')

    agent = int(server.seen(answered, "the orchestrator's reply", 600 + ROLE_TIMEOUT).actor.partition('#')[2])
    running = server.seen(kind_on('agent_session.running', 'agent_session', agent), f'orchestrator#{agent} ran', 60)
    thread = server.api.thread(sent.comment.id)
    [reply, *_] = [c for c in thread.linked if c.author.startswith('orchestrator#')]
    assert reply.in_reply_to == sent.comment.id, thread
    server.seen(
        kind_on('comment.acked_at', 'comment', sent.comment.id), 'the orchestrator acknowledges the message', 600
    )
    assert server.api.thread(sent.comment.id).comment.acked_by == reply.author
    [acked] = server.api.ack(AckBody(ids=(reply.id,))).results
    assert acked.outcome is AckOutcome.ACKED, acked
    print(
        f'comment#{sent.comment.id} by {sent.comment.author}, answered on {payload(running)["machine"]} by {reply.author}:'
        f' {reply.body[:200]}'
    )


@needs(test_the_orchestrator_adds_an_idea, test_a_person_adds_an_idea_that_is_open_on_the_board)
def test_implementers_run_on_the_servers_workers(server: Server) -> None:
    """Each free slot queues a run from the best. Its implementer names its harness session, runs on a machine of the
    server, and claims an idea of the board, proposes one of its own, or exits without one: its run ends ``gave_up``."""
    for _ in range(MAX_CONCURRENT):
        queued = server.seen(
            lambda e: e.kind == 'run.queued' and e.subject.id not in server.runs, 'a run on a free slot', 300
        )
        run = queued.subject.id
        identified = server.seen(
            lambda e, r=run: e.kind == 'agent_session.identified' and payload(e)['subject'] == f'run#{r}',
            f'run#{run} agent',
            600,
        )
        agent = identified.subject.id
        running = server.seen(kind_on('agent_session.running', 'agent_session', agent), f'agent#{agent} running', 900)
        first = server.seen(
            lambda e, a=agent, r=run: (
                (e.kind == 'idea.status' and payload(e)['new'] == 'claimed' and e.actor == f'implementer#{a}')
                or (e.kind == 'run.finished' and e.subject.id == r)
            ),
            f'the idea that implementer#{agent} claims, or the end of run#{run}',
            ROLE_TIMEOUT,
        )
        if first.kind == 'run.finished':
            assert payload(first)['end'] == 'gave_up', f'run#{run} ended before a claim: {payload(first)}'
            server.runs[run] = Claim(agent=agent, idea=None, proposed=False)
            print(f'run#{run}: agent#{agent} on {payload(running)["machine"]} gave up without an idea')
            continue
        idea = first.subject.id
        proposed = any(e.subject.id == idea and e.actor == f'implementer#{agent}' for e in server.of('idea.created'))
        server.runs[run] = Claim(agent=agent, idea=idea, proposed=proposed)
        how = 'proposed' if proposed else 'claimed'
        print(f'run#{run}: agent#{agent} on {payload(running)["machine"]} {how} idea#{idea}')


@needs(test_implementers_run_on_the_servers_workers)
def test_candidates_are_measured_and_scored(server: Server) -> None:
    """Each run ends, by its worker's ``run end`` or its clean exit: ``finished`` when it submitted, ``gave_up`` else. A
    finished run settles its idea as done; one that gave up opens it again. At least one run submits, and its submission
    is scored into the pool."""
    settles = {'finished': 'done', 'gave_up': 'open'}
    for run, claim in server.runs.items():
        ended = payload(server.seen(kind_on('run.finished', 'run', run), f'run#{run} finished', RUNS_WAIT))
        assert ended['end'] in settles, f'run#{run}: {ended}'
        submitted = [payload(e)['candidate'] for e in server.of('run.submitted') if e.subject.id == run]
        assert bool(submitted) == (ended['end'] == 'finished'), f'run#{run} {ended["end"]}, submitted {submitted}'
        if claim.idea is not None:
            server.seen(
                lambda e, i=claim.idea, s=settles[str(ended['end'])]: (
                    e.kind == 'idea.status' and e.subject.id == i and payload(e)['new'] == s
                ),
                f'idea#{claim.idea} {settles[str(ended["end"])]}',
                600,
            )
        print(f'run#{run}: {ended["end"]}, submitted {submitted}; idea#{claim.idea}')
    assert any(e.subject.id in server.runs for e in server.of('run.submitted')), 'no run submitted'

    def from_runs(e: Envelope) -> bool:
        return e.kind == 'measurement.recorded' and payload(e)['run'] in server.runs

    measured = server.seen(from_runs, 'a measurement of a run', 600)
    candidate = payload(measured)['candidate']
    assert isinstance(candidate, int)
    scored = payload(server.seen(kind_on('candidate.scored', 'candidate', candidate), 'its score', 600))
    print(f'candidate#{candidate} of run#{payload(measured)["run"]}: {payload(measured)["metrics"]}, {scored["told"]}')


@needs(test_candidates_are_measured_and_scored)
def test_every_agent_saved_its_session_file(server: Server) -> None:
    """Each agent that named its harness session stored the file in the server's CAS (``agent_session.session_saved``)."""
    identified = {e.subject.id: payload(e)['session_id'] for e in server.of('agent_session.identified')}
    for agent in identified:
        server.seen(kind_on('agent_session.finished', 'agent_session', agent), f'agent#{agent} finished', 600)
    saved = {e.subject.id: payload(e) for e in server.of('agent_session.session_saved')}
    assert identified.keys() <= saved.keys(), f'no session file of agents {identified.keys() - saved.keys()}'
    for agent, session in identified.items():
        assert saved[agent]['session_id'] == session, saved[agent]
        print(f'agent#{agent} session {session}: {saved[agent]["session_file"]}')


@needs(test_measures_the_baseline)
def test_pause_and_resume(server: Server) -> None:
    server.piloop('pause', server.name, '--reason', 'e2e: pause and resume')
    paused = server.seen(lambda e: e.kind == 'campaign.paused' and payload(e)['new'] is not None, 'the pause', 60)
    assert server.state().paused == payload(paused)['new']
    server.piloop('resume', server.name)
    server.seen(lambda e: e.kind == 'campaign.paused' and payload(e)['new'] is None, 'the resume', 60)
    assert server.state().paused is None


@needs(test_registers_a_repository_by_its_url)
def test_stop(server: Server) -> None:
    """The orchestrator's ``piloop campaign stop`` is refused: only a person or a stop rule stops a campaign. The
    person's stop closes it, and wakes no orchestrator; once the agents that ran end, none starts."""
    with pytest.raises(CommandFailed, match='may not stop a campaign: only a person or a stop rule does'):
        server.piloop('campaign', 'stop', server.name, '--reason', 'e2e: the orchestrator asks')
    if server.state().status == 'active':  # the stop rule's max_runs can stop it first
        server.api.stop(str(server.id), StopBody(reason='e2e: the scenario ends', ending=Ending.WRAP_UP))
    stopped = server.seen(lambda e: e.kind == 'campaign.status' and payload(e)['new'] == 'stopped', 'the stop', 120)
    server.seen(lambda e: e.kind == 'campaign.closed', 'the campaign closed', ROLE_TIMEOUT + 300)
    asked = {e.subject.id for e in server.of('agent_session.requested') if e.id < stopped.id}
    for agent in sorted(asked - {e.subject.id for e in server.of('agent_session.finished')}):
        server.seen(
            kind_on('agent_session.finished', 'agent_session', agent), f'agent#{agent} ends', ROLE_TIMEOUT + 300
        )
    server.none(
        lambda e: e.id > stopped.id and e.kind in {'agent_session.requested', 'agent_session.started'},
        'an agent after the stop',
        QUIET,
    )
    state = server.state()
    assert state.status == 'stopped', state
    assert state.agents == [], state.agents
    orchestrator = state.roles[RoleName('orchestrator')]
    assert (orchestrator.requested, orchestrator.live) == (0, 0), orchestrator
    print(f'{server.name}: best {state.best}; no agent after the stop')


@needs(test_stop)
def test_fork(server: Server) -> None:
    """``piloop campaign fork``: a new campaign starts from the stopped one's best, under it, with the same repo."""
    assert server.state().best
    out = server.piloop('campaign', 'fork', server.name)
    print(out.strip())
    [forked] = server.api.status(Selection(campaign=f'{server.name}-2')).campaigns
    assert isinstance(forked, CampaignState), forked
    server.forked = forked.id
    assert (forked.status, forked.parent and forked.parent.id) == ('active', server.id), forked


@needs(test_stop)
def test_reopen(server: Server) -> None:
    """``piloop campaign reopen``: the stopped campaign is active again; the person's second stop closes it again, and
    wakes no orchestrator."""
    server.piloop('campaign', 'reopen', server.name, '--reason', 'e2e: one more round')
    server.seen(lambda e: e.kind == 'campaign.reopened', 'the reopen', 60)
    assert server.state().status == 'active'
    stop = server.cursor
    server.api.stop(str(server.id), StopBody(reason='e2e: stop again', ending=Ending.NOW))
    closed = server.seen(lambda e: e.id > stop and e.kind == 'campaign.closed', 'the second close', ROLE_TIMEOUT + 300)
    server.none(
        lambda e: e.id > stop and e.kind == 'agent_session.requested' and payload(e)['role'] == 'orchestrator',
        'an orchestrator that the second stop woke',
        QUIET,
    )
    state = server.state()
    assert state.status == 'stopped', state
    print(f'{server.name}: closed again at event {closed.id}; no orchestrator woke')


@needs(test_stop)
def test_retire_frees_the_name_and_the_url_registers_again(server: Server) -> None:
    """``piloop campaign retire``: the stopped campaign leaves the listing and its name.

    A second registration of the URL under that name is a new campaign, in a checkout of its own.
    """
    server.piloop('campaign', 'retire', server.name, '--reason', 'e2e: the scenario ends')
    state = server.state()
    assert state.status == 'retired', state
    assert server.name not in [c.name for c in server.api.status(Selection()).campaigns]
    again = server.api.register_campaign(Clone(clone=Remote(url=server.repo), name=server.name)).campaign
    assert isinstance(again, CampaignState), again
    server.again = again.id
    assert (again.name, again.status) == (server.name, 'active'), again
    assert again.id != server.id and again.path != state.path, again
    print(f'{server.name}: retired #{server.id} at {state.path}; registered again as #{again.id} at {again.path}')
