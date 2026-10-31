"""A role with ``continuity = "resume"``: each agent continues the last agent's harness session, from the CAS."""

import json
import shutil
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from covalence import Cid
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select

from piloop.actor import Agent
from piloop.agents.launcher import resumable
from piloop.agents.results import AgentSessionStarted, Ran, SessionNotResumed, Unresumed
from piloop.agents.roles import compose_brief, compose_resumed, role
from piloop.db.events import in_order
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Event
from piloop.db.session import Database
from piloop.ids import AgentSessionId, TargetKind, TargetRef
from piloop.otel import Tracer
from piloop.plugins.campaign_type.kit.runs.controller import RunController
from piloop.plugins.campaign_type.kit.runs.ids import IMPLEMENTER
from piloop.plugins.campaign_type.kit.runs.tables import working_on
from piloop.plugins.cas.disk import DiskCas
from piloop.plugins.executor.contract import RepoId, Resume
from piloop.plugins.executor.kit import jobs
from piloop.plugins.harness.claude import Claude
from piloop.plugins.harness.contract import Usage
from piloop.plugins.harness.pi import Pi
from piloop.plugins.host import Instances
from piloop.plugins.role.contract import Continuity
from piloop.plugins.runtime.contract import AgentStart
from piloop.plugins.vcs.contract import Repository
from piloop.store.chunks import read as read_chunks
from tests.agents.test_launcher import agent_session, checkout, controller, request, use_harness
from tests.conftest import (
    WATCHDOG,
    ScriptRuntime,
    events,
    home_cas,
    reconfigured,
    role_timeout,
    wait_until,
    workers_of,
)
from tests.daemon.test_run_controller import WORKED, controller as run_controller, execute, make_run
from tests.test_lease import agent_job
from tests.usage.test_usage import SESSIONS

SESSION = SESSIONS / 'claude.jsonl'
"""Two responses: the first agent writes the head with the first, and the resumed one appends the second."""
HOME = '~/claude-home'
HARNESS = {
    'provider': 'claude',
    'home': HOME,
    'headless': ['--session-id', '{session_id}', '{prompt}'],
    'resume': ['--resume', '{session_id}', '{prompt}'],
}
"""The ``claude`` provider over the test script: it reads and restores Claude session files under ``HOME``."""


def harness(tmp_path: Path) -> str:
    """Starts the session with the head of ``SESSION``, or resumes it: its file must be there, and it gets the rest.

    Each start keeps the brief that it read, by its mode, so the test reads what the agent got.
    """
    lines = SESSION.read_bytes().splitlines(keepends=True)
    (tmp_path / 'head.jsonl').write_bytes(b''.join(lines[:5]))
    (tmp_path / 'rest.jsonl').write_bytes(b''.join(lines[5:]))
    return (
        '#!/bin/sh\n'
        'D="$HOME/claude-home/projects/$(pwd | sed \'s/[^a-zA-Z0-9]/-/g\')"\n'
        'mkdir -p "$D"\n'
        f'cp ../brief.md "{tmp_path}/brief$1.md"\n'
        'case "$1" in\n'
        f'  --session-id) cp {tmp_path / "head.jsonl"} "$D/$2.jsonl" ;;\n'
        f'  --resume) [ -f "$D/$2.jsonl" ] || exit 3; cat {tmp_path / "rest.jsonl"} >> "$D/$2.jsonl" ;;\n'
        'esac\n'
    )


def resuming(db: Database, campaign: Campaign) -> None:
    """Make the watchdog a role that resumes its sessions."""
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.config = reconfigured(row.config, plugins={'role': {WATCHDOG: {'continuity': Continuity.RESUME.value}}})
        s.commit()


def wake(db: Database, runtime: ScriptRuntime, tmp_path: Path, campaign: Campaign) -> AgentSessionId:
    """Launch one watchdog with both briefs, as the scheduler launches a sweep of a resuming role."""
    agent_session_id = request(db, campaign)
    ctl = controller(db, runtime, tmp_path, Tracer(None))
    launch(db, ctl, campaign, agent_session_id)
    ctl.stop(5)
    return agent_session_id


def launch(db: Database, ctl: RunController, campaign: Campaign, agent_session_id: AgentSessionId) -> None:
    """Launch the watchdog ``agent_session_id`` on ``ctl``, and wait for its end."""
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        agent = Agent(role=WATCHDOG, session=agent_session_id)
        watchdog = role(row.config, WATCHDOG, Path(row.path))
        brief = compose_brief(watchdog, row, '- the whole brief')
        resumed = compose_resumed(row.config.role(WATCHDOG), agent, f'- news for {agent}')
    ctl.launcher.launch(
        agent_session_id,
        brief=ctl.cas.put(brief.encode()),
        place=f'sweeps/{agent_session_id}',
        artifact=checkout(campaign),
        timeout=role_timeout(db, agent_session_id),
        resumed_brief=ctl.cas.put(resumed.encode()),
    )


def not_resumed(db: Database, agent_session_id: AgentSessionId) -> list[SessionNotResumed]:
    with db.session() as s:
        on = Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id)
        return [e for _, e in in_order(s, SessionNotResumed, on)]


def started(db: Database, agent_session_id: AgentSessionId) -> AgentSessionStarted:
    with db.session() as s:
        on = Event.target == TargetRef(TargetKind.AGENT_SESSION, agent_session_id)
        [(_, event)] = in_order(s, AgentSessionStarted, on)
        return event


def unread(_: RepoId) -> Repository:
    raise AssertionError('restoring a session opens no repo of record')


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    return home


def test_each_agent_resumes_the_last_session_with_only_the_new_brief_and_counts_only_its_own_tokens(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, home: Path
) -> None:
    use_harness(db, campaign, harness(tmp_path), **HARNESS)
    resuming(db, campaign)
    first = wake(db, runtime, tmp_path, campaign)
    assert not_resumed(db, first) == [
        SessionNotResumed(reason=Unresumed.NO_SESSION, detail='no earlier watchdog left a session on script')
    ]
    assert '- the whole brief' in (tmp_path / 'brief--session-id.md').read_text()
    one = agent_session(db, first)
    assert one.state is AgentSessionState.EXITED and one.session_id is not None and one.session_file is not None
    assert one.usage is not None and one.usage.turns == 1

    (folder,) = (home / 'claude-home' / 'projects').iterdir()
    for left in folder.iterdir():
        left.unlink()  # the next worker is another machine: only the CAS has the session
    second = wake(db, runtime, tmp_path, campaign)
    two = agent_session(db, second)
    assert (two.state, two.error, not_resumed(db, second)) == (AgentSessionState.EXITED, None, [])
    assert started(db, second).resumes == first
    assert two.session_id == one.session_id, 'the same harness session'
    brief = (tmp_path / 'brief--resume.md').read_text()
    assert brief.startswith(f'You continue this session as a new agent, watchdog#{second}.')
    assert f'- news for watchdog#{second}' in brief and '- the whole brief' not in brief
    assert two.usage == Usage(input=2, output=1649, cache_read=52633, cache_write=3969, total=58253, turns=1), (
        'the tokens after the resume: the whole file less what it held'
    )
    assert two.session_file is not None and home_cas(db).get(two.session_file) is not None
    third = wake(db, runtime, tmp_path, campaign)
    assert (started(db, third).resumes, not_resumed(db, third)) == (second, []), 'a resume that ended well goes on'


PI_SESSION = SESSIONS / 'pi.jsonl'
PI_HARNESS = {
    'provider': 'pi',
    'home': '~/pi-home',
    'headless': ['--session-id', '{session_id}', '{prompt}'],
    'resume': ['--session-id', '{session_id}', '{prompt}'],
}
"""The ``pi`` provider over the test script: it reads and restores pi session files under ``~/pi-home``."""


def pi_harness(tmp_path: Path) -> str:
    """pi's ``--session-id``: it continues the session of that id under the folder of its cwd, else starts one.

    As pi does, it refuses a session whose header names another cwd. It keeps the brief that it read, by whether it
    resumed.
    """
    lines = PI_SESSION.read_bytes().splitlines(keepends=True)
    (tmp_path / 'head.jsonl').write_bytes(b''.join(lines[:5]))
    (tmp_path / 'rest.jsonl').write_bytes(b''.join(lines[5:]))
    return (
        '#!/bin/sh\n'
        'D="$HOME/pi-home/sessions/--$(pwd | sed \'s|^/||; s|[/:]|-|g\')--"\n'
        'mkdir -p "$D"\n'
        'F=$(ls "$D"/*_"$2".jsonl 2>/dev/null)\n'
        'if [ -z "$F" ]; then\n'
        f'  cp ../brief.md "{tmp_path}/brief-new.md"\n'
        f'  sed "1s|\\"cwd\\":\\"[^\\"]*\\"|\\"cwd\\":\\"$(pwd)\\"|" {tmp_path / "head.jsonl"} > "$D/2026-09-09T05-48-14-300Z_$2.jsonl"\n'
        'else\n'
        '  head -1 "$F" | grep -qF "\\"cwd\\":\\"$(pwd)\\"" || exit 1\n'
        f'  cp ../brief.md "{tmp_path}/brief-resumed.md"\n'
        f'  cat {tmp_path / "rest.jsonl"} >> "$F"\n'
        'fi\n'
    )


def test_a_pi_agent_resumes_the_last_session_on_another_worker_in_its_own_cwd(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, home: Path
) -> None:
    use_harness(db, campaign, pi_harness(tmp_path), **PI_HARNESS)
    resuming(db, campaign)
    first = wake(db, runtime, tmp_path, campaign)
    one = agent_session(db, first)
    assert one.state is AgentSessionState.EXITED and one.session_file is not None
    sessions = home / 'pi-home' / 'sessions'
    for folder in sessions.iterdir():
        shutil.rmtree(folder)  # the next worker is another machine: only the CAS has the session

    second = wake(db, runtime, tmp_path, campaign)
    two = agent_session(db, second)
    assert (two.state, two.error, not_resumed(db, second)) == (AgentSessionState.EXITED, None, [])
    assert (started(db, second).resumes, two.session_id) == (first, one.session_id)
    assert f'- news for watchdog#{second}' in (tmp_path / 'brief-resumed.md').read_text()
    assert len(list(sessions.glob('*/*.jsonl'))) == 1, 'pi continued the file that piloop wrote back'
    assert two.usage is not None and two.usage.turns == 1, 'the tokens after the resume'


CODEX_HARNESS = {
    'provider': 'codex',
    'home': '~/codex-home',
    'headless': ['{prompt}'],
    'resume': ['resume', '{session_id}', '{prompt}'],
}
"""The ``codex`` provider over the test script: it reads and restores Codex rollouts under ``~/codex-home``."""
CODEX = f'''#!{sys.executable}
"""Codex as 0.165 behaves: a resume continues the rollout that its state names, else the one its thread id finds.

Each response adds 1000 tokens to the thread's running total, which a resume continues from the rollout it loads.
The response comes after a delay, as Codex's own start takes seconds.
"""
import json, os, sys, time, uuid
from pathlib import Path

home = Path(os.environ['HOME']) / 'codex-home'
if sys.argv[1] == 'resume':
    thread = sys.argv[2]
    state = home / 'state' / thread
    found = sorted((home / 'sessions').glob(f'*/*/*/rollout-*-{{thread}}.jsonl'))
    rollout = Path(state.read_text()) if state.exists() and Path(state.read_text()).exists() else found[0]
else:
    thread = str(uuid.uuid4())
    rollout = home / 'sessions/2026/10/28' / f'rollout-2026-10-28T00-00-00-{{thread}}.jsonl'
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(json.dumps({{'type': 'session_meta', 'payload': {{'id': thread}}}}) + '\\n')
print(json.dumps({{'type': 'thread.started', 'thread_id': thread}}, separators=(',', ':')), flush=True)
state = home / 'state' / thread
state.parent.mkdir(exist_ok=True)
state.write_text(str(rollout))
time.sleep(float(os.environ.get('CODEX_START_SECONDS', '0')))
while os.path.exists(os.environ.get('CODEX_HOLD', '')):
    time.sleep(0.05)
counts = [json.loads(line)['payload']['info']['total_token_usage']['total_tokens']
          for line in rollout.read_text().splitlines() if '"token_count"' in line]
total = (counts[-1] if counts else 0) + 1000
usage = {{'input_tokens': total - total // 10, 'cached_input_tokens': 0, 'output_tokens': total // 10,
         'total_tokens': total}}
with rollout.open('a') as f:
    f.write(json.dumps({{'type': 'event_msg', 'payload': {{'type': 'token_count', 'info': {{'total_token_usage': usage}}}}}}) + '\\n')
'''


def rollout_total(path: Path) -> int:
    """The running total of the last ``token_count`` of a rollout: what its thread spent in all."""
    counts = [line for line in path.read_text().splitlines() if '"token_count"' in line]
    return json.loads(counts[-1])['payload']['info']['total_token_usage']['total_tokens']


def totals(db: Database, *agent_sessions: AgentSessionId) -> list[int]:
    found = [agent_session(db, a).usage for a in agent_sessions]
    return [-1 if usage is None else usage.total for usage in found]


@pytest.fixture
def codex(db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """The fake Codex for a resuming watchdog. It starts after the job code's first look at its session file."""
    monkeypatch.setattr(jobs, 'POLL_SECONDS', 0.05)
    monkeypatch.setenv('CODEX_START_SECONDS', '0.3')
    use_harness(db, campaign, CODEX, **CODEX_HARNESS)
    resuming(db, campaign)
    return home / 'codex-home'


def test_codex_resumes_count_each_agents_own_tokens_and_add_up_to_the_rollouts_total(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, codex: Path
) -> None:
    agents = [wake(db, runtime, tmp_path, campaign) for _ in range(3)]
    assert [started(db, a).resumes for a in agents] == [None, *agents[:2]]
    (rollout,) = codex.glob('sessions/*/*/*/rollout-*.jsonl')
    assert rollout_total(rollout) == 3000, 'Codex continued its own rollout, which the state names'
    assert totals(db, *agents) == [1000, 1000, 1000]
    last = agent_session(db, agents[-1]).session_file
    assert last is not None and read_chunks(home_cas(db), last) == rollout.read_bytes(), 'the CAS has the rollout'


def test_codex_resumes_on_other_machines_continue_the_cas_session_and_count_each_agents_own_tokens(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, codex: Path
) -> None:
    """Machine A starts the session, B resumes it from the CAS alone, then A resumes it again with its old rollout."""
    first = wake(db, runtime, tmp_path, campaign)
    codex.rename(tmp_path / 'machine-a')
    second = wake(db, runtime, tmp_path, campaign)
    (on_b,) = codex.glob('sessions/*/*/*/rollout-*.jsonl')
    assert rollout_total(on_b) == 2000
    shutil.rmtree(codex)
    (tmp_path / 'machine-a').rename(codex)
    third = wake(db, runtime, tmp_path, campaign)
    (on_a,) = codex.glob('sessions/*/*/*/rollout-*.jsonl')
    assert rollout_total(on_a) == 3000, 'A continued the session as B left it, in the rollout that its state names'
    assert totals(db, first, second, third) == [1000, 1000, 1000]


def test_a_resumed_codex_agent_that_a_restarted_worker_re_attaches_counts_only_its_own_tokens(
    campaign: Campaign,
    db: Database,
    runtime: ScriptRuntime,
    tmp_path: Path,
    codex: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = wake(db, runtime, tmp_path, campaign)
    hold = tmp_path / 'hold'
    hold.touch()
    monkeypatch.setenv('CODEX_HOLD', str(hold))
    second = request(db, campaign)
    before = controller(db, runtime, tmp_path, Tracer(None))
    launching = threading.Thread(target=launch, args=(db, before, campaign, second))
    launching.start()
    wait_until(lambda: agent_session(db, second).state is AgentSessionState.RUNNING)
    before.hand_over()
    launching.join(30)
    after = controller(db, runtime, tmp_path, Tracer(None))
    workers_of(after).leases.renew_all()
    workers_of(after).worker.start()
    hold.unlink()
    assert isinstance(after.launcher.reattach(second), Ran)
    after.stop(5)
    (rollout,) = codex.glob('sessions/*/*/*/rollout-*.jsonl')
    assert (rollout_total(rollout), totals(db, first, second)) == (2000, [1000, 1000])


def test_the_tokens_that_a_resumed_session_held_are_found_again_from_the_job_alone(
    db: Database, campaign: Campaign, tmp_path: Path
) -> None:
    """A re-attached agent knows only its job: the session file in its ``resume`` says what was not the agent's."""
    cas = home_cas(db)
    site = jobs.local_site(tmp_path / 'worker', cas, lambda _: Path(campaign.path), unread, 0)
    reader = Claude(home=HOME).sessions()
    session = cas.put(SESSION.read_bytes())
    resume = Resume(session_id='s1', file=session, brief=cas.put(b'# new'), args=['--resume', 's1'])
    job = agent_job(db, campaign, tmp_path).model_copy(update={'harness': Claude(home=HOME), 'resume': resume})
    assert jobs.inherited(job, 's1', reader, site) == reader.usage(SESSION.read_bytes())
    assert jobs.inherited(job, 'fresh', reader, site) is None, 'a session that could not be written back is new'
    assert jobs.inherited(job.model_copy(update={'resume': None}), 's1', reader, site) is None
    no_response = resume.model_copy(update={'file': cas.put(b'{"type":"session"}\n')})
    assert jobs.inherited(job.model_copy(update={'resume': no_response}), 's1', reader, site) is None, (
        "every token is the agent's"
    )
    lost = resume.model_copy(update={'file': Cid.of(b'never put')})
    with pytest.raises(LookupError, match='the CAS lacks the resumed session file'):
        jobs.inherited(job.model_copy(update={'resume': lost}), 's1', reader, site)


def home_cas_disk(db: Database) -> DiskCas:
    cas = home_cas(db)
    assert isinstance(cas, DiskCas)
    return cas


def test_a_session_that_the_worker_cannot_write_back_starts_anew_with_the_whole_brief_and_says_why(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, home: Path
) -> None:
    use_harness(db, campaign, harness(tmp_path), **HARNESS)
    resuming(db, campaign)
    first = wake(db, runtime, tmp_path, campaign)
    session_file = agent_session(db, first).session_file
    assert session_file is not None
    home_cas_disk(db).release(session_file)
    second = wake(db, runtime, tmp_path, campaign)
    [why] = not_resumed(db, second)
    assert why.reason is Unresumed.UNRESTORED and str(session_file) in why.detail
    two = agent_session(db, second)
    assert two.state is AgentSessionState.EXITED and two.session_id != agent_session(db, first).session_id
    assert '- the whole brief' in (tmp_path / 'brief--session-id.md').read_text()


def test_no_session_is_resumed_after_a_resume_that_failed_or_on_a_harness_that_cannot(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, home: Path
) -> None:
    use_harness(db, campaign, harness(tmp_path), **HARNESS)
    resuming(db, campaign)
    first = wake(db, runtime, tmp_path, campaign)
    with db.session() as s:
        cfg = s.get_one(Campaign, campaign.id).config
        later = s.get_one(AgentSession, request(db, campaign))
        found = resumable(s, later, cfg, Path('w'))
        assert not isinstance(found, SessionNotResumed) and found.agent_session == first
        other = s.get_one(AgentSession, request(db, campaign, harness='other'))
        assert resumable(s, other, cfg, Path('w')) == SessionNotResumed(
            reason=Unresumed.NO_SESSION, detail='no earlier watchdog left a session on other'
        )
        harnesses = cfg.plugins['harness']
        assert isinstance(harnesses, Instances)
        bare = cfg.harness['script'].model_copy(update={'resume': []})
        with_bare = replace(harnesses, instances={'script': bare})
        assert resumable(
            s, later, cfg.model_copy(update={'plugins': {**cfg.plugins, 'harness': with_bare}}), Path('w')
        ) == (
            SessionNotResumed(
                reason=Unresumed.HARNESS, detail='script has no resume argv, or its provider restores no session file'
            )
        )
        without = replace(harnesses, instances={})
        assert resumable(
            s, later, cfg.model_copy(update={'plugins': {**cfg.plugins, 'harness': without}}), Path('w')
        ) == SessionNotResumed(reason=Unresumed.HARNESS, detail='piloop.toml no longer names script')

    use_harness(db, campaign, harness(tmp_path).replace('[ -f "$D/$2.jsonl" ] || exit 3', 'exit 3'), **HARNESS)
    resuming(db, campaign)
    broken = wake(db, runtime, tmp_path, campaign)
    assert started(db, broken).resumes == first and agent_session(db, broken).error is not None
    with db.session() as s:
        s.get_one(AgentSession, broken).state = AgentSessionState.KILLED
        s.commit()
    after = wake(db, runtime, tmp_path, campaign)
    assert not_resumed(db, after) == [
        SessionNotResumed(reason=Unresumed.FAILED, detail=f'agent_session#{broken} resumed a session and ended killed')
    ]
    with db.session() as s:
        assert s.scalar(select(AgentSession.state).where(AgentSession.id == after)) is AgentSessionState.EXITED


def test_a_worker_that_cannot_restore_says_why(
    db: Database, campaign: Campaign, tmp_path: Path, home: Path, tracer: Tracer, spans: InMemorySpanExporter
) -> None:
    cas = home_cas(db)
    site = jobs.local_site(tmp_path / 'worker', cas, lambda _: Path(campaign.path), unread, 0)
    session = cas.put(b'{"type":"session"}\n')
    resume = Resume(session_id='s1', file=session, brief=cas.put(b'# new'), args=['--resume', 's1'])
    claude = Claude(home=HOME)
    job = agent_job(db, campaign, tmp_path).model_copy(update={'harness': claude, 'resume': resume})
    start = AgentStart(executable='x', args=['--session-id', 'fresh'])
    with tracer.span('test') as span:
        (site.log_dir(job)).mkdir(parents=True)
        restored, resumed_start, outcome = jobs.restore_session(job, start, site, span)
        assert (restored.session_id, resumed_start.args, outcome) == ('s1', ['--resume', 's1'], None)
        cases = {
            'the harness has no path': job.model_copy(update={'harness': None}),
            'no session header': job.model_copy(
                update={'harness': Pi(), 'resume': resume.model_copy(update={'file': cas.put(b'{"type":"message"}\n')})}
            ),
            'the CAS lacks the resumed brief': job.model_copy(
                update={'resume': resume.model_copy(update={'brief': Cid.of(b'never put')})}
            ),
        }
        for why, broken in cases.items():
            kept, same, failed = jobs.restore_session(broken, start, site, span)
            assert (kept, same) == (broken, start)
            assert isinstance(failed, jobs.Unrestored) and why in failed.reason
    assert len(events(spans, jobs.SESSION_UNRESTORED)) == 3


def test_an_implementer_of_a_resuming_role_continues_the_last_runs_session_in_its_own_new_run(
    campaign: Campaign, db: Database, runtime: ScriptRuntime, tmp_path: Path, home: Path
) -> None:
    """``continuity = "resume"`` on a role that does runs: each run's agent continues the session of the last run's.

    It gets the role's resume message and only the run's context and sections: the prompt is in the session already.
    """
    use_harness(db, campaign, harness(tmp_path), **HARNESS)
    with db.session() as s:
        row = s.get_one(Campaign, campaign.id)
        row.config = reconfigured(row.config, plugins={'role': {IMPLEMENTER: {'continuity': Continuity.RESUME.value}}})
        s.commit()
    first = make_run(db, campaign, title='first')
    execute(run_controller(db, runtime, tmp_path, agents=WORKED), db, first)
    assert '# Implementer' in (tmp_path / 'brief--session-id.md').read_text(), (
        'the first run starts from its whole brief'
    )
    second = make_run(db, campaign, title='second')
    execute(run_controller(db, runtime, tmp_path, agents=WORKED), db, second)
    with db.session() as s:
        one, two = (s.scalars(select(AgentSession).where(working_on(r))).one() for r in (first, second))
        resumes, two_id = started(db, two.id).resumes, two.id
        assert (resumes, two.session_id, not_resumed(db, two.id)) == (one.id, one.session_id, [])
    brief = (tmp_path / 'brief--resume.md').read_text()
    assert brief.startswith(f'You continue this session as a new agent, implementer#{two_id}.')
    assert '- Your cwd is your checkout' in brief and '# Implementer' not in brief


def test_an_implementer_role_starts_each_run_in_a_fresh_session_by_default(campaign: Campaign) -> None:
    assert campaign.config.role(IMPLEMENTER).continuity is Continuity.FRESH
