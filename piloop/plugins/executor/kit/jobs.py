"""Run one job on this machine: an agent with its runtime. A job of another kind runs as its kind says (``JobKind.run``).

Any executor can use it (``piloop.plugins.executor.contract``): ``workers`` does, for each job its worker takes, and so
can one that runs jobs inline. What the executor does around a job (track it, stop it, report it) is its
``Control``; the machine it runs on is its ``Machine``. Like Airflow's ``run_workload``: one way to run a unit of work,
whichever executor placed it.

The steps of an agent's job: make its place and its artifact (a checkout, ``Clones``), write the brief, put the values
of its secrets in its environment, render the MCP servers, start the agent, watch it to its end, save its transcript,
put its session file in the CAS, read its usage, and bring its versions back. What it made goes back by address: the report names
its versions, its log files (``Machine.files``) and what it wrote in the shared workspace in the CAS. The checkout stays:
the executor removes it (``remove``) once the report is out, so a worker that restarts in between finds the ended agent
there.
"""

import os
import shlex
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path

from covalence import Cid
from pydantic import SecretStr

from piloop import deprecated
from piloop.config import BRIEF_FILE, TRANSCRIPT
from piloop.ids import CommentId, EnvName
from piloop.otel import Span
from piloop.plugins.cas.contract import Cas
from piloop.plugins.executor.contract import (
    DEADLINE_ENV,
    AgentFinished,
    AgentJob,
    AgentNotStarted,
    AgentReport,
    Control,
    Job,
    JobSecrets,
    Prompt,
    RepoId,
    Site,
)
from piloop.plugins.executor.kit.clones import Clones, Fetch, of_record, own_replica
from piloop.plugins.executor.kit.relay import RELAY_ARGV
from piloop.plugins.executor.kit.sessions import POLL_SECONDS, SessionFile
from piloop.plugins.executor.kit.transfers import send_of_record
from piloop.plugins.harness.contract import MCP, McpLaunch, Usage, UsageReader, expand
from piloop.plugins.mcp_server.http import HttpServer
from piloop.plugins.runtime.contract import (
    AgentExit,
    AgentSpec,
    AgentStart,
    Deadline,
    ExitStatus,
    Handle,
    Runtime,
    taken,
)
from piloop.plugins.vcs.contract import Repository
from piloop.proc import AGENT_TOKEN_ENV, PILOOP_ARGV, child_env
from piloop.settings import INPUTS_DIR
from piloop.store.chunks import read as read_chunks
from piloop.store.tree import Materialised, Seen, evict_unlinked, put_tree
from piloop.usage import UsageError

TRANSCRIPT_LINES = 2000
STOPPED = 'the worker stopped before the job started'
WORKTREE = 'worktree'
"""The checkout of a job's ``Source``, in its place."""
CLONES = 'clones'
"""Where a worker elsewhere keeps its clones, under its home (``clone_of``)."""
INPUTS_REBUILT = 'inputs.rebuilt'
"""An event: a shared copy of a linked input no longer held its blob (a job changed it), and was made again."""
LOGIN_CARRIED = 'agent_session.login_carried'
"""An isolated agent took the login keys of the user's harness settings, which piloop passes on until 0.5.0: set them
in the harness's ``env``."""
SESSION_UNSAVED = 'agent_session.session_unsaved'
"""An event: a snapshot of an agent's session file did not go into the CAS. The next one tries again."""
SESSION_UNRESTORED = 'agent_session.session_unrestored'
"""An event: the session file of a job's ``resume`` could not be written back, and the agent starts a new session."""
SHOWN = 10
"""How many paths or blobs an event or an error names."""
CLI_SHIM = '#!/bin/sh\nPILOOP_HOME={home} exec {argv} "$@"\n'
"""``<home>/bin/piloop``: this home's piloop, the worker's own code on the worker's home, or on a worker elsewhere the
relay to the server (``door.main``). Agents get it as ``PILOOP_BIN``, which the briefs use, and its directory first on
their PATH, for the prompts' plain ``piloop``. Another install can come first on the user's PATH (a release next to a
build of ``main``), and a harness's shell can run the user's rc files again, which can put it back in front."""


def own_cli(home: Path, argv: Sequence[str]) -> Path:
    """Write the ``CLI_SHIM`` that runs ``argv`` under ``home``, and return its path."""
    shim = home / 'bin' / 'piloop'
    text = CLI_SHIM.format(home=shlex.quote(str(home)), argv=shlex.join(argv))
    if not shim.is_file() or shim.read_text() != text:
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(text)
        shim.chmod(0o755)
    return shim


@dataclass
class Machine:
    """The machine that runs jobs: its home, under which each job's place is made, its CAS, and its clones.

    It is the ``Site`` that a job kind's ``run`` gets.
    """

    home: Path
    cas: Cas
    clones: Clones
    reserve: int
    """Bytes kept free on the disk of ``home`` (``[storage] min_free_gb``): ``evict_inputs`` frees room below it."""
    piloop: Sequence[str] = PILOOP_ARGV
    """What an agent's ``piloop`` runs: this process's piloop, or on a worker elsewhere the relay to the server."""
    cli: Path = field(init=False)
    seen: Seen = field(default_factory=dict, init=False)
    """What ``files`` stored, so that a file it stored before is not read again."""

    def __post_init__(self) -> None:
        self.cli = own_cli(self.home, self.piloop)

    def log_dir(self, job: Job) -> Path:
        """The job's place on this machine: its log files, and its worktree."""
        return self.home / job.place

    def cwd(self, job: Job) -> Path:
        """Where the job runs: its checkout."""
        return self.log_dir(job) / WORKTREE

    def env(self) -> dict[str, str]:
        """What an agent's environment gets from this machine: its piloop first on the PATH, and its home.

        ``PILOOP_HOME`` too, although the shim sets it: a harness shell that runs the user's rc files can put another
        install's piloop first on PATH, and with the variable that one still uses this home's database.
        """
        path = f'{self.cli.parent}{os.pathsep}{child_env()["PATH"]}'
        return {'PILOOP_HOME': str(self.home), 'PILOOP_BIN': str(self.cli), 'PATH': path}

    def evict_inputs(self) -> int:
        """Under ``reserve``, remove the copies of linked inputs that no worktree links, as ``make_room`` does.

        One eviction (``evict_unlinked``): a worker elsewhere has no archiver to run ``make_room``. Return how many went.
        """
        return evict_unlinked(self.home / INPUTS_DIR, self.reserve)

    def checkout(self, job: Job) -> Materialised:
        """Make the job's checkout of its artifact (``cwd``)."""
        return self.clones.checkout(job.artifact, self.cwd(job))

    def remove(self, job: Job) -> None:
        """Remove the job's checkout, if it made one."""
        self.clones.remove(job.artifact, self.cwd(job))

    def files(self, job: Job) -> dict[str, Cid]:
        """Put the files of the job's log directory, its checkout aside, into the CAS, and return them by path."""
        log_dir = self.log_dir(job)
        names = [name for name in os.listdir(log_dir) if name != WORKTREE] if log_dir.is_dir() else []
        stored = put_tree(self.cas, log_dir, names, seen=self.seen)
        return {e.path: e.blob for e in stored.manifest.entries if e.blob is not None}


def local_site(
    home: Path, cas: Cas, repos: Callable[[RepoId], Path], records: Callable[[RepoId], Repository], reserve: int
) -> Machine:
    """The machine of the daemon's own process: its home and CAS, with the repos of record as its clones.

    A version that a clone lacks goes from the repo of record into the CAS, as the server does for a remote worker.
    """
    return Machine(home, cas, Clones(cas, repos, of_record, partial(send_of_record, cas, records), home), reserve)


def remote_site(home: Path, cas: Cas, fetch: Fetch, reserve: int) -> Machine:
    """The machine of a worker elsewhere: its home and the shared CAS, with clones of its own, filled by ``fetch``.

    Its agents' ``piloop`` is the relay to the server (``door.main``).
    """
    return Machine(home, cas, Clones(cas, partial(clone_of, home), own_replica, fetch, home), reserve, RELAY_ARGV)


def clone_of(home: Path, repo: RepoId) -> Path:
    """Where the worker keeps its clone of a repo of record, ``<home>/clones/<repo>`` (``Vcs.clone``)."""
    return home / CLONES / str(repo)


def run_agent(job: AgentJob, runtime: Runtime, control: Control, site: Machine, span: Span) -> AgentReport | None:
    """Make the artifact, start the agent, watch it to its end, and return its report.

    None when there is nothing to report: the job is no longer the executor's, or the daemon hands it over.
    """
    started_at = time.monotonic()
    log_dir, cwd = site.log_dir(job), site.cwd(job)
    try:
        rebuilt(site.checkout(job), span)
        log_dir.mkdir(parents=True, exist_ok=True)
        if site.cas.get_file(job.brief_cid, log_dir / BRIEF_FILE) is None:
            raise LookupError(f'the CAS lacks the brief {job.brief_cid}')
        job, start, unrestored = restore_session(job, job.spec, site, span)
        spec = rendered(job, start.at(cwd, log_dir, site.env()), control.secrets(), span)
        with closing(site.clones.repository(job.artifact)) as repo:
            spec = replace(spec, shares=(*spec.shares, repo.store()))
    except Exception as e:  # a full disk, a locked index, a missing secret: a fault of the host, not of the agent
        span.error(e)
        return _unstarted(job, site, f'{type(e).__name__}: {e}', started_at)
    if not control.begin():
        site.remove(job)
        return AgentNotStarted(reason=STOPPED)
    handle: Handle | None = None
    try:
        started_at = time.monotonic()
        try:
            handle = runtime.start(replace(spec, env=spec.env | {DEADLINE_ENV: str(int(time.time() + job.timeout))}))
        except Exception as e:  # a missing executable, a full disk: the agent did not start to work
            span.error(e)
            return _unstarted(job, site, f'{type(e).__name__}: {e}', started_at)
        try:
            recorded = control.started(handle, job.session_id)
        except Exception as e:
            # Not recorded, so nothing would ever watch the agent or wait for its process: end it here.
            span.error(e)
            runtime.kill(handle)
            handle = None
            return _unstarted(job, site, f'{type(e).__name__}: {e}', started_at)
        if not recorded:  # the job is no longer the executor's: nothing waits for this agent
            runtime.kill(handle)
            handle = None
            site.remove(job)
            return None
    finally:
        control.running(None if handle is None else _killer(runtime, handle))
    assert handle is not None
    return watch_agent(job, runtime, handle, 0, control, site, span, unrestored)


@dataclass(frozen=True)
class Unrestored:
    """The session file of the job's ``resume`` could not be written back: the agent starts a new session."""

    reason: str


def restore_session(
    job: AgentJob, start: AgentStart, site: Machine, span: Span
) -> tuple[AgentJob, AgentStart, Unrestored | None]:
    """The job and the start of an agent, after its ``resume``: its file and brief written back and the resume argv.

    A resumed job takes the resumed session's id. Else the job, its start and its whole brief stay as they are, and
    ``Unrestored`` says why. None: the job resumes nothing, or its session was written back.
    """
    if (resume := job.resume) is None:
        return job, start, None
    cwd = site.cwd(job)
    try:
        if job.harness is None or (path := job.harness.restore(resume.session_id, cwd)) is None:
            raise LookupError('the harness has no path to write a session file back to (Harness.restore)')
        if (data := read_chunks(site.cas, resume.file)) is None:
            raise LookupError(f'the CAS lacks the session file {resume.file}')
        brief = site.cas.get(resume.brief)
        if brief is None:
            raise LookupError(f'the CAS lacks the resumed brief {resume.brief}')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(job.harness.relocated(data, cwd))
        (site.log_dir(job) / BRIEF_FILE).write_bytes(brief)
    except Exception as e:  # a missing or broken blob, a full disk: the agent starts anew, and its report says why
        span.event(SESSION_UNRESTORED, agent_session=job.agent_session, error=f'{type(e).__name__}: {e}')
        return job, start, Unrestored(f'{type(e).__name__}: {e}')
    restored = job.model_copy(update={'session_id': resume.session_id})
    return restored, replace(start, args=resume.args), None


def watch_agent(
    job: AgentJob,
    runtime: Runtime,
    handle: Handle,
    elapsed: float,
    control: Control,
    site: Machine,
    span: Span,
    unrestored: Unrestored | None = None,
) -> AgentFinished | None:
    """Watch a started agent to its end, then return its report. ``elapsed``: the seconds it ran before this.

    None after a hand-over: the next daemon re-attaches, and the job and its worktree stay. ``unrestored``: why
    ``restore_session`` could not write the session back at the start, which a re-attach no longer knows.
    """
    reader = job.harness.usage() if job.harness is not None else None
    file = None if reader is None else SessionFile(reader, site.cas, site.cwd(job), job.session_id)
    deadline = Deadline(time.monotonic() - elapsed, job.timeout, control.extended)
    outcome, session_id = _watch(job, runtime, handle, deadline, control, site, file, span)
    if control.handing_over():
        return None
    outcome = replace(outcome, seconds=elapsed + outcome.seconds)
    return _finish(job, outcome, session_id, control, site, file, span, unrestored)


def inherited(job: AgentJob, session_id: str | None, reader: UsageReader, site: Machine) -> Usage | None:
    """The usage that the session held when the agent resumed it: its file in the CAS, as the worker wrote it back.

    It is not the agent's. None when the agent continues no session: its job resumes none, or it started a new one
    because the file could not be written back. A re-attached agent finds it again.

    Raises:
        LookupError: The CAS lacks the file.
    """
    if (resume := job.resume) is None or session_id != resume.session_id:
        return None
    if (data := read_chunks(site.cas, resume.file)) is None:
        raise LookupError(f'the CAS lacks the resumed session file {resume.file}')
    try:
        return reader.usage(data)
    except UsageError:  # a session without a response yet: every token is the agent's
        return None


def _killer(runtime: Runtime, handle: Handle) -> Callable[[], None]:
    return lambda: runtime.kill(handle)


def _unstarted(job: AgentJob, site: Machine, error: str, started_at: float) -> AgentNotStarted:
    """Remove the worktree of an agent that did not start, and return its report: a failed start."""
    site.remove(job)
    return AgentNotStarted(reason=error, seconds=time.monotonic() - started_at, files=site.files(job))


def _watch(
    job: AgentJob,
    runtime: Runtime,
    handle: Handle,
    deadline: Deadline,
    control: Control,
    site: Machine,
    file: SessionFile | None,
    span: Span,
) -> tuple[AgentExit, str | None]:
    """Wait for a started agent to end, save its transcript and release it. Return how, and its session id.

    When the harness reports its session id, record it: asked before the wait, again before each snapshot of its
    session file while it runs (``_snapshots``), and again before the agent is released, because a harness that is
    slow to start reports its session late.
    """
    session_id = job.session_id
    reported = not job.needs_session_id or session_id is not None
    started_at = time.monotonic()
    stop, snapshots = threading.Event(), None
    try:
        if not reported:
            session_id = _session(runtime, handle, control, span)
            reported = session_id is not None
        if file is not None:
            file.session_id = session_id
            snapshots = threading.Thread(
                target=_snapshots,
                args=(job, runtime, handle, control, file, span, stop),
                name=f'snapshots-{job.agent_session}',
                daemon=True,
            )
            snapshots.start()
        outcome = runtime.wait(handle, deadline)
    except Exception as e:  # a pane or a server that is gone: the agent ended for the worker
        span.error(e)
        outcome = AgentExit(ExitStatus.EXITED, time.monotonic() - started_at, error=f'{type(e).__name__}: {e}')
    finally:
        stop.set()
        if snapshots is not None:  # a snapshot that is being put ends first: the last one comes after it
            snapshots.join()
        if not reported and file is not None and file.session_id is not None:  # a snapshot asked for it
            session_id, reported = file.session_id, True
        if not reported:
            try:
                if (session_id := _session(runtime, handle, control, span)) is None:
                    span.event('agent_session.session_unknown', agent_session=job.agent_session, runtime=handle.runtime)
            except Exception as e:  # the pane or the server can be gone; releasing the agent comes first
                span.event(
                    'agent_session.session_unknown',
                    agent_session=job.agent_session,
                    runtime=handle.runtime,
                    error=str(e),
                )
        if not control.handing_over():
            try:
                (site.log_dir(job) / TRANSCRIPT).write_text(runtime.read(handle, lines=TRANSCRIPT_LINES))
            except Exception as e:  # the agent can already be gone; a missing transcript is not an error
                span.event('agent_session.transcript_lost', error=str(e))
            runtime.kill(handle)
    return outcome, session_id  # after the last ask: a session reported late goes in the report


def _snapshots(
    job: AgentJob,
    runtime: Runtime,
    handle: Handle,
    control: Control,
    file: SessionFile,
    span: Span,
    stop: threading.Event,
) -> None:
    """Look at the agent's session file every ``POLL_SECONDS`` until ``stop``, and record each new snapshot.

    A snapshot is due as ``SessionFile.due`` says. One that fails is tried again at the next look.
    """
    while not stop.wait(POLL_SECONDS):
        try:
            if file.session_id is None:
                file.session_id = _session(runtime, handle, control, span)
            now, before = time.monotonic(), file.latest
            if file.due(now) and (latest := file.snapshot(now)) is not None and latest != before:
                control.saved(latest)
        except Exception as e:  # a file gone, a CAS that does not answer
            span.event(SESSION_UNSAVED, agent_session=job.agent_session, error=f'{type(e).__name__}: {e}')


def _session(runtime: Runtime, handle: Handle, control: Control, span: Span) -> str | None:
    session_id = runtime.session_id(handle)
    if session_id is not None:
        span.set(session_id=session_id)
        control.session(session_id)
    return session_id


def _finish(
    job: AgentJob,
    outcome: AgentExit,
    session_id: str | None,
    control: Control,
    site: Machine,
    file: SessionFile | None,
    span: Span,
    unrestored: Unrestored | None,
) -> AgentFinished:
    """Take the last snapshot of the session file, read the usage, bring the versions back, and return the report."""
    span.set(session_id=session_id)
    log_dir, cwd = site.log_dir(job), site.cwd(job)
    session_file, usage = None, None
    if file is not None:
        file.session_id = session_id
        try:
            file.snapshot(time.monotonic())
        except Exception as e:  # a file gone, a CAS that does not answer: the report names the snapshot before
            span.event(SESSION_UNSAVED, agent_session=job.agent_session, error=f'{type(e).__name__}: {e}')
        session_file = file.latest
        try:
            usage = file.reader.read(session_id, cwd, log_dir).since(inherited(job, session_id, file.reader, site))
            span.set(tokens=usage.total)
        except Exception as e:  # an unreadable file or a bug in a reader must not stop the report
            error = str(e) if isinstance(e, UsageError) else f'{type(e).__name__}: {e}'
            span.event('agent_session.usage_unknown', agent_session=job.agent_session, error=error)
    changed, captured, workspace = True, None, {}
    try:
        changed = job.artifact.vcs.changed(cwd, site.clones.ignored(job.artifact))
        if job.capture is not None:
            captured = site.clones.bring_back(job.artifact, cwd, job.capture)
        workspace = site.clones.changed(job.artifact, cwd)
    except Exception as e:  # a worktree that is gone: the report says what is known
        span.error(e)
        outcome = replace(outcome, error=outcome.error or f'{type(e).__name__}: {e}')
    read, unread = _reads(control.queued(), log_dir)
    work = captured.work if captured is not None else None
    span.set(status=outcome.status, error=outcome.error, head=work.head if work is not None else None)
    return AgentFinished(
        process=outcome,
        session_id=session_id,
        session_file=session_file,
        usage=usage,
        unrestored=unrestored.reason if unrestored is not None else None,
        changed=changed,
        work=work,
        unsaved=captured.unsaved if captured is not None else None,
        read=read,
        unread=unread,
        workspace=workspace,
        files=site.files(job),
    )


def _reads(queued: list[list[Prompt]], log_dir: Path) -> tuple[list[CommentId], list[CommentId]]:
    """Split the ``queued`` prompts into those that the agent took from its queue and those that wait in it (``taken``).

    Each ``Runtime.prompt`` is one message of the queue, so the messages that wait are the newest prompts, and the ones
    taken came just before. A runtime without the queue leaves both empty: its prompts stay queued.
    """
    read, waiting = taken(log_dir)
    split = max(len(queued) - waiting, 0)
    return [p.id for b in queued[max(split - read, 0) : split] for p in b], [p.id for b in queued[split:] for p in b]


@deprecated.until('0.5.0', 'remove(job, site) of 0.3.1 is site.remove(job): delete remove')
def remove(job: Job, site: Site) -> None:
    """Remove the job's worktree, if it made one: ``Site.remove``."""
    site.remove(job)


def rebuilt(links: Materialised, span: Span) -> Materialised:
    """Record the blobs whose shared copy a job had changed: they were made again for this one."""
    if links.rebuilt:
        span.event(INPUTS_REBUILT, blobs=', '.join(str(blob) for blob in links.rebuilt[:SHOWN]))
    return links


class MissingSecrets(LookupError):
    """An agent's job needs secrets that the home does not have. The message names them, never a value."""


def rendered(job: AgentJob, spec: AgentSpec, found: JobSecrets, span: Span) -> AgentSpec:
    """The spec with the job's secrets and token from ``found`` (``Control.secrets``), and ``{mcp}`` expanded.

    It also names what a runtime that isolates the agent must let through: its harness's sessions, and its servers.

    piloop's own variables in the spec and the token win over a secret of the same name, and both over the login that
    an isolated agent takes from the user's setup (``Harness.carried``), which ``span`` notes.

    Raises:
        MissingSecrets: a secret is not set.
        McpError: the servers cannot be given to this agent.
    """
    env = (
        secret_env(job.secrets, found.values, child_env())
        | spec.env
        | {AGENT_TOKEN_ENV: found.token.get_secret_value()}
    )
    if job.harness is not None and (carried := job.harness.carried(spec.args, spec.resume_args)) is not None:
        deprecated.until(
            '0.5.0', "auth keys of a user's harness settings pass to an isolated agent: delete this branch"
        )
        span.event(LOGIN_CARRIED, keys=', '.join(carried.names))
        spec = replace(spec, args=carried.args, resume_args=carried.resume_args)
        env = carried.env | env
    reader = job.harness.sessions() if job.harness is not None else None
    spec = replace(
        spec,
        shares=() if reader is None else (reader.root(),),
        reaches=tuple(s.address for s in job.servers.values() if isinstance(s, HttpServer)),
    )
    renderer = job.harness.mcp() if job.harness is not None else None
    if renderer is None or MCP not in [*spec.args, *spec.resume_args]:
        return replace(spec, env=env)
    mcp = renderer.render(McpLaunch(job.servers, spec.executable, spec.cwd, spec.log_dir, child_env() | env, spec.args))
    return replace(spec, args=expand(spec.args, mcp), resume_args=expand(spec.resume_args, mcp), env=env | mcp.env)


def secret_env(
    wanted: Mapping[EnvName, EnvName], found: Mapping[str, SecretStr], machine: Mapping[str, str]
) -> dict[str, str]:
    """The variables of ``wanted`` (``AgentJob.secrets``), each with the value of its secret in ``found``.

    Raises:
        MissingSecrets: a secret is neither in ``found`` nor, for now, in ``machine``, the environment of this machine.
    """
    env: dict[str, str] = {}
    missing: list[str] = []
    for variable, secret in wanted.items():
        match found.get(secret):
            case SecretStr() as value:
                env[variable] = value.get_secret_value()
            case None if secret in machine:
                deprecated.until(
                    '0.5.0',
                    'a secret from the environment of the machine that runs the job: delete this case,'
                    ' Settings.secrets_command, Daemon._load_secrets and the environment in doctor._secrets_of',
                )
                env[variable] = machine[secret]
            case _:
                missing.append(secret)
    if missing:
        raise MissingSecrets(f'the secrets {", ".join(sorted(set(missing)))} are not set: `piloop secret set <NAME>`')
    return env
