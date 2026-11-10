import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest
from git import Repo
from inline_snapshot import snapshot
from pytest_mock import MockerFixture
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from piloop.actor import System
from piloop.agents.results import CampaignResults
from piloop.config import load_campaign_config
from piloop.daemon.main import ACTOR, Daemon
from piloop.daemon.serve import LOCK_ENV, RESTART_SIGNAL, SERVE_ARGV, running_pid
from piloop.db.events import latest
from piloop.db.models import AgentSession, AgentSessionState, Campaign, Event
from piloop.db.session import Database
from piloop.ids import Revision, TargetRef
from piloop.plugins.campaign_type.kit.backlog.ids import IdeaId, IdeaStatus
from piloop.plugins.campaign_type.kit.backlog.tables import Idea
from piloop.plugins.campaign_type.kit.candidates.ids import CANDIDATE, CandidateId, Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.pool.pool import best_of
from piloop.plugins.campaign_type.kit.pool.scored import CandidateScored
from piloop.plugins.campaign_type.kit.runs.ids import RunEnd, RunId, RunStatus
from piloop.plugins.campaign_type.kit.runs.tables import Run
from piloop.plugins.executor.kit.transfers import BUILDS
from piloop.proc import Inherited, alive, started
from piloop.settings import Settings
from tests.conftest import (
    AUDITOR,
    END_RUN,
    HARNESS_APPEND,
    HARNESS_FAILS,
    SETTLE,
    WRITE_PID,
    ScriptRuntime,
    bare_config,
    claim,
    console_lines,
    end,
    factory,
    queued,
    register,
    run_all,
    serving_api,
    settle,
    trace,
    wait_until,
    workers_of,
    write_script,
    write_toml,
)
from tests.daemon.test_scheduler import run_closed
from tests.daemon.test_submissions import submitted


def scored(s: Session, candidate: CandidateId) -> bool:
    """Whether the daemon scored ``candidate`` into the pool."""
    return latest(s, CandidateScored, Event.target == TargetRef(CANDIDATE, candidate)) is not None


def test_daemon_end_to_end(
    campaign_path: Path, tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv('PILOOP_AGENTS__STARTUP_SECONDS', '0')  # the failing implementer worked, then left nothing
    home = tmp_path / 'home'
    runtime = ScriptRuntime()
    daemon = Daemon(Settings(home=home))
    write_script(campaign_path.parent / 'harness.sh', HARNESS_APPEND + END_RUN)
    with serving_api(Settings(home=home)):  # each worker's `piloop run end` reaches it
        mocker.patch.object(workers_of(daemon.controller).worker, 'runtime_factory', factory(runtime))
        campaign = register(daemon.db, campaign_path)
        with daemon.db.session() as s:
            s.add(Idea(track='score', campaign_id=campaign.id, title='ok', hypothesis='ok', created_by='t'))
            s.commit()

        daemon.scheduler.reap()
        assert queued(daemon.db, daemon.scheduler, campaign.id, IdeaId(1)) == [1]
        assert run_all(daemon.scheduler) == [1]
        wait_until(lambda: run_closed(daemon.db, RunId(1)))  # the trace is in order: its submission is scored after
        settle(daemon.scheduler, daemon.db)
        with daemon.db.session() as s:
            run = s.get_one(Run, 1)
            assert (run.status, run.updated_by) == (RunStatus.SUCCEEDED, ACTOR.signature)
            assert s.get_one(Idea, 1).status is IdeaStatus.DONE

        write_toml(campaign_path, HARNESS_FAILS)
        with daemon.db.session() as s:
            s.get_one(Campaign, campaign.id).config = load_campaign_config(campaign_path)
            s.commit()
        assert run_all(daemon.scheduler) == [2], 'a fresh run fills the free slot'
        daemon.scheduler.wait(SETTLE)
        assert trace(daemon.db) == snapshot(
            [
                'idea.created idea',
                'lease_worker.joined home',
                'agent_session.requested agent_session',
                'run.queued run',
                'idea.status idea',
                'run.started run',
                'agent_session.started agent_session',
                'lease.queued agent_session',
                'lease.acquired agent_session',
                'lease.started agent_session',
                'agent_session.running agent_session',
                'run.submitted run',
                'run.end_requested run',
                'lease.reported agent_session',
                'agent_session.finished agent_session',
                'agent_session.job_ended agent_session',
                'run.received run',
                'run.finished run',
                'lease.queued candidate',
                'lease.acquired candidate',
                'lease.started candidate',
                'lease.reported candidate',
                'evaluation.deleted candidate',
                'measurement.recorded measurement',
                'candidate.scored candidate',
                'idea.status idea',
                'subject.settled idea',
                'agent_session.requested agent_session',
                'run.queued run',
                'run.started run',
                'agent_session.started agent_session',
                'lease.queued agent_session',
                'lease.acquired agent_session',
                'lease.started agent_session',
                'agent_session.running agent_session',
                'lease.reported agent_session',
                'agent_session.finished agent_session',
                'agent_session.job_ended agent_session',
                'run.received run',
                'run.finished run',
            ]
        ), 'what the first two runs wrote: a claimed run settles its idea; a fresh run that claims nothing settles none'
        write_toml(campaign_path, HARNESS_APPEND + END_RUN)
        with daemon.db.session() as s:
            s.get_one(Campaign, campaign.id).config = load_campaign_config(campaign_path)
            s.commit()
        assert run_all(daemon.scheduler) == [3], 'a fresh run fills the free slot: the failure wakes no agent'
        daemon.scheduler.wait(SETTLE)
        daemon.shutdown(timeout=5)


def test_serve_runs_until_interrupted(tmp_path: Path, mocker: MockerFixture) -> None:
    daemon = Daemon(Settings(home=tmp_path / 'home'))
    loop = mocker.patch.object(daemon.scheduler, 'serve', side_effect=KeyboardInterrupt)
    daemon.serve()
    assert loop.called
    assert not (tmp_path / 'home' / 'serve.pid').exists()


def test_serve_reaps_the_children_an_in_place_restart_handed_over_as_each_exits(tmp_path: Path) -> None:
    early = os.posix_spawn('/bin/sh', ['sh', '-c', 'exit 0'], os.environ)
    late = os.posix_spawn('/bin/sh', ['sh', '-c', 'sleep 0.8'], os.environ)
    daemon = Daemon(Settings(home=tmp_path / 'home'), Inherited({early, late}))

    def reaped(pid: int) -> bool:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return True
        return False

    def stop_once_reaped() -> None:
        wait_until(lambda: reaped(early) and reaped(late))
        daemon.scheduler.interrupt()

    stopping = threading.Thread(target=stop_once_reaped)
    stopping.start()
    daemon.serve()  # signals need the main thread
    stopping.join(30)
    assert reaped(early) and reaped(late)


def test_serve_stops_on_sigterm_and_restores_the_handler(tmp_path: Path, mocker: MockerFixture) -> None:
    cfg = Settings(home=tmp_path / 'home')
    daemon = Daemon(cfg)
    previous = signal.getsignal(signal.SIGTERM)
    served = {'n': 0}

    def serve() -> None:
        served['n'] += 1
        assert cfg.pid_file.read_text() == str(os.getpid())
        assert signal.getsignal(signal.SIGTERM) is not previous
        os.kill(os.getpid(), signal.SIGTERM)
        real_serve()  # the handler interrupted it: the loop returns

    def stop(timeout: float) -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # a second SIGTERM during shutdown must not fire the default action
        real_stop(timeout)

    real_serve, real_stop = daemon.scheduler.serve, daemon.scheduler.stop
    mocker.patch.object(daemon.scheduler, 'serve', side_effect=serve)
    mocker.patch.object(daemon.scheduler, 'stop', side_effect=stop)
    daemon.serve()
    assert served['n'] == 1
    assert signal.getsignal(signal.SIGTERM) is previous
    assert not cfg.pid_file.exists()


def test_the_restart_signal_hands_the_agents_over_then_execs_serve_in_place(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    cfg = Settings(home=tmp_path / 'home')
    daemon = Daemon(cfg)
    previous = signal.getsignal(RESTART_SIGNAL)
    execv = mocker.patch('piloop.daemon.main.os.execv')
    shutdown = mocker.spy(daemon, 'shutdown')
    hand_over, stop = mocker.spy(daemon.scheduler, 'hand_over'), mocker.spy(daemon.scheduler, 'stop')

    def serve() -> None:
        assert not execv.called
        os.kill(os.getpid(), RESTART_SIGNAL)

    mocker.patch.object(daemon.scheduler, 'serve', side_effect=serve)
    daemon.serve()
    assert shutdown.called, 'shut down as on SIGTERM'
    handed = int(os.environ.pop(LOCK_ENV))
    assert running_pid(cfg) == os.getpid() and os.get_inheritable(handed), 'the lock goes on to the exec'
    os.close(handed)
    assert hand_over.called and not stop.called, 'its agents are left for the new code'
    assert execv.call_args.args == (SERVE_ARGV[0], SERVE_ARGV)
    assert signal.getsignal(RESTART_SIGNAL) is previous


def test_a_restart_whose_shutdown_fails_still_execs(tmp_path: Path, mocker: MockerFixture) -> None:
    """A restart execs the new code even when its shutdown raises, for example on a locked database.

    Without the exec, the threads of the runs in flight keep the old process and its lock alive. The new daemon's
    ``reap`` fails the runs that the old one could not. A stop on SIGTERM that raises keeps the lock instead.
    """
    cfg = Settings(home=tmp_path / 'home')
    daemon = Daemon(cfg)
    execv = mocker.patch('piloop.daemon.main.os.execv')
    mocker.patch.object(daemon.scheduler, 'serve', side_effect=lambda: os.kill(os.getpid(), RESTART_SIGNAL))
    mocker.patch.object(daemon.scheduler, 'hand_over', side_effect=OperationalError('SELECT', {}, Exception('locked')))
    with pytest.raises(OperationalError):
        daemon.serve()
    worker = workers_of(daemon.controller).worker
    worker.stop()  # the failed shutdown left the worker that the reap started: its loop ends before the database
    worker.join(5)
    daemon.db.close()
    assert execv.call_args.args == (SERVE_ARGV[0], SERVE_ARGV)
    os.close(int(os.environ.pop(LOCK_ENV)))  # handed on to an exec that the mock left out

    execv.reset_mock()
    cfg = Settings(home=tmp_path / 'stopped')
    daemon = Daemon(cfg)
    previous = signal.getsignal(signal.SIGTERM)
    mocker.patch.object(daemon.scheduler, 'serve', side_effect=lambda: os.kill(os.getpid(), signal.SIGTERM))
    mocker.patch.object(daemon.scheduler, 'stop', side_effect=OperationalError('INSERT', {}, Exception('locked')))
    with pytest.raises(OperationalError):
        daemon.serve()
    worker = workers_of(daemon.controller).worker
    worker.stop()  # the failed shutdown left the worker that the reap started: its loop ends before the database
    worker.join(5)
    daemon.db.close()
    assert not execv.called and cfg.pid_file.exists(), 'a stop keeps the lock while its run threads may live on'
    assert signal.getsignal(signal.SIGTERM) is previous, 'a second SIGTERM ends what is left'


HARNESS_HELD = (
    f'#!/bin/sh\necho a >> solve.txt\ngit commit -qam first\n{WRITE_PID}'
    'while [ -f {hold} ]; do sleep 0.05; done\necho b >> solve.txt\ngit commit -qam second\n'
)
"""An implementer that commits, says it is under way, waits while the test holds it, then commits again."""


def test_an_in_place_restart_leaves_the_agents_running_and_the_new_daemon_finishes_their_runs(
    campaign_path: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    """An in-place restart does not kill the agents, and the new daemon finishes their runs.

    An agent is a process in its own session, so it outlives the exec. The new daemon picks
    it up where it is, and the idea does not run again.
    """
    hold = tmp_path / 'hold'
    hold.touch()
    write_toml(campaign_path, HARNESS_HELD.format(hold=hold) + END_RUN, implementer_timeout=60)
    cfg = Settings(home=tmp_path / 'home')
    first = Daemon(cfg)
    campaign = register(first.db, campaign_path)
    with first.db.session() as s:
        s.add(Idea(track='score', campaign_id=campaign.id, title='two steps', hypothesis='x', created_by='t'))
        s.commit()
    execv = mocker.patch('piloop.daemon.main.os.execv')
    under_way = cfg.home / 'runs' / '1' / 'agent.pid'

    def restart_once_under_way() -> None:
        wait_until(under_way.exists)
        claim(first.db, RunId(1), IdeaId(1))
        os.kill(os.getpid(), RESTART_SIGNAL)

    threading.Thread(target=restart_once_under_way, daemon=True).start()
    first.serve()
    assert execv.called
    agent = int(under_way.read_text())
    start = started(agent)
    assert start is not None and alive(agent, start), 'the agent is still running'
    after = Database(cfg.database_url, actor=System.HUMAN)  # the first daemon closed its own
    with after.session() as s:
        assert (s.get_one(Run, 1).status, s.get_one(AgentSession, 1).state) == (
            RunStatus.RUNNING,
            AgentSessionState.RUNNING,
        )
    assert (cfg.home / 'runs' / '1' / 'worktree').is_dir()

    after.close()
    second = Daemon(cfg)
    second.scheduler.reap()
    with serving_api(cfg):  # the agent's `piloop submit` and `piloop run end` reach the new daemon
        hold.unlink()
        settle(second.scheduler, second.db)
    with second.db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.error, s.get_one(AgentSession, 1).state) == (
            RunStatus.SUCCEEDED,
            None,
            AgentSessionState.EXITED,
        )
        made = s.scalars(select(Candidate).where(Candidate.run_id == 1).order_by(Candidate.id)).all()
        assert [(c.summary, c.origin, scored(s, c.id)) for c in made] == [
            ('second', Origin.RUN, True),
            ('first', Origin.SKIPPED, False),
        ], 'its submission is scored; the commit before it is kept unsubmitted'
        best = best_of(s, s.get_one(Campaign, campaign.id))
        assert best is not None and best.summary == 'second'
        assert s.get_one(Idea, 1).status is IdeaStatus.DONE
        assert s.scalar(select(func.count()).select_from(Run)) == 1, 'the idea never ran again'
    wait_until(lambda: not (cfg.home / 'runs' / '1' / 'worktree').exists())  # once the agent's result is in
    second.shutdown(timeout=5)
    # In a real restart the exec ends the old thread. Here it lives on, sees its agent end, and leaves the run alone.
    first.scheduler.wait(SETTLE)


@pytest.mark.parametrize(
    ('ended', 'run_end', 'idea_status', 'is_scored'),
    [
        (False, (RunStatus.FAILED, RunEnd.DAEMON_STOPPED), IdeaStatus.OPEN, False),
        (True, (RunStatus.SUCCEEDED, RunEnd.FINISHED), IdeaStatus.DONE, True),
    ],
    ids=['cut', 'handed off'],
)
def test_a_stop_that_ends_before_it_writes_the_rows_still_ends_the_run_when_the_next_daemon_reads_the_report(
    campaign_path: Path,
    tmp_path: Path,
    mocker: MockerFixture,
    ended: bool,
    run_end: tuple[RunStatus, RunEnd],
    idea_status: IdeaStatus,
    is_scored: bool,
) -> None:
    """The stop is on the agent's row before its kill, so the next daemon reads the report as the stop's.

    The first daemon stops its agent, cannot apply the report (a locked database), and dies before it writes the rows.
    The next daemon adopts the agent and applies the spooled report: the run is cut by the stop, not failed, and its
    commit is kept unmeasured. A run whose implementer submitted its commit and ended it is handed off, and its
    submission is scored.
    """
    hold = tmp_path / 'hold'
    hold.touch()
    write_toml(campaign_path, HARNESS_HELD.format(hold=hold), implementer_timeout=60)
    cfg = Settings(home=tmp_path / 'home')
    first = Daemon(cfg)
    campaign = register(first.db, campaign_path)
    with first.db.session() as s:
        s.add(Idea(track='score', campaign_id=campaign.id, title='two steps', hypothesis='x', created_by='t'))
        s.commit()
    first.scheduler.reap()
    assert run_all(first.scheduler) == [1]
    wait_until((cfg.home / 'runs' / '1' / 'agent.pid').exists)
    claim(first.db, RunId(1), IdeaId(1))
    if ended:
        submitted(first.db, campaign, RunId(1), Revision(Repo(cfg.home / 'runs' / '1' / 'worktree').head.commit.hexsha))
        end(first.db, RunId(1))
    locked = mocker.patch.object(CampaignResults, 'finished', side_effect=OperationalError('', {}, Exception('locked')))
    first.controller.stop(timeout=SETTLE)
    assert locked.called, 'the killed agent reported, and its report waits in the spool'
    mocker.stopall()
    first.controller.launcher.handover.set()  # its run thread writes nothing either
    first.db.notify()
    first.scheduler.wait(SETTLE)
    first.db.close()
    os.close(first._lock)  # its process dies here: the kernel frees the lock

    second = Daemon(cfg)
    second.scheduler.reap()
    settle(second.scheduler, second.db)
    with second.db.session() as s:
        run, agent = s.get_one(Run, 1), s.get_one(AgentSession, 1)
        assert ((run.status, run.end), agent.state, agent.error) == (
            run_end,
            AgentSessionState.KILLED,
            RunEnd.DAEMON_STOPPED,
        )
        assert s.get_one(Idea, 1).status is idea_status, 'a cut run is not an attempt'
        [kept] = s.scalars(select(Candidate).where(Candidate.run_id == 1)).all()
        assert (kept.summary, scored(s, kept.id)) == ('first', is_scored)
    second.shutdown(timeout=5)


def test_a_report_that_comes_after_the_stop_ended_keeps_its_work_for_the_next_daemon(
    campaign_path: Path, tmp_path: Path, mocker: MockerFixture
) -> None:
    """A stop whose agent's report came after its wait used to refuse the report and fail the run from its old base.

    The stop leaves such an agent to the next daemon, as a crash does: the next daemon applies the spooled report, keeps
    its commit as recovered, and the run's claim ends: its idea is open again.
    """
    hold = tmp_path / 'hold'
    hold.touch()
    write_toml(campaign_path, HARNESS_HELD.format(hold=hold), implementer_timeout=60)
    cfg = Settings(home=tmp_path / 'home')
    first = Daemon(cfg)
    campaign = register(first.db, campaign_path)
    with first.db.session() as s:
        s.add(Idea(track='score', campaign_id=campaign.id, title='two steps', hypothesis='x', created_by='t'))
        s.commit()
    first.scheduler.reap()
    assert run_all(first.scheduler) == [1]
    wait_until((cfg.home / 'runs' / '1' / 'agent.pid').exists)
    claim(first.db, RunId(1), IdeaId(1))
    locked = mocker.patch.object(CampaignResults, 'finished', side_effect=OperationalError('', {}, Exception('locked')))
    first.shutdown(timeout=1)
    assert locked.called, 'the killed agent reported, and its report waits in the spool'
    mocker.stopall()

    second = Daemon(cfg)
    second.scheduler.reap()
    second.scheduler.wait(SETTLE)
    with second.db.session() as s:
        run = s.get_one(Run, 1)
        assert (run.status, run.end) == (RunStatus.FAILED, RunEnd.DAEMON_STOPPED)
        [kept] = s.scalars(select(Candidate).where(Candidate.run_id == 1)).all()
        idea = s.get_one(Idea, 1)
        assert (kept.summary, idea.status) == ('first', IdeaStatus.OPEN)
    second.shutdown(timeout=5)


def test_serve_refuses_when_a_daemon_holds_the_pid_file(tmp_path: Path, lock_holder: subprocess.Popen[str]) -> None:
    """The lock comes first: the refused daemon opens no database, so it migrates and stops nothing of the other's."""
    cfg = Settings(home=tmp_path)
    with pytest.raises(SystemExit, match=f'already running \\(pid {lock_holder.pid}'):
        Daemon(cfg)
    assert cfg.pid_file.read_text() == str(lock_holder.pid) and not (tmp_path / 'piloop.db').exists()


@pytest.mark.usefixtures('auditor')
def test_shutdown_fails_open_runs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    daemon = Daemon(Settings(home=tmp_path / 'home'))
    with daemon.db.session() as s:
        s.add(Campaign(name='p', path='/p', config=bare_config('p')))
        s.add(Idea(track='score', campaign_id=1, title='x', hypothesis='x', created_by='t'))
        s.add(
            Run(
                campaign_id=1,
                idea_id=1,
                attempt=1,
                base_candidate_id=1,
                harness='h',
                model='m',
                status=RunStatus.RUNNING,
            )
        )
        s.flush()  # the run first: the unit of work orders unrelated mappers by name
        s.add(AgentSession(kind=AUDITOR, campaign_id=1, reason='by hand'))
        s.commit()
    daemon.shutdown(timeout=1)
    after = Database(daemon.db.engine.url.render_as_string(hide_password=False), actor=System.HUMAN)
    with after.session() as s:
        assert (s.get_one(Run, 1).status, s.get_one(Run, 1).end) == (RunStatus.FAILED, RunEnd.DAEMON_STOPPED)
        [request] = s.scalars(select(AgentSession)).all()
        assert request.state is AgentSessionState.REQUESTED, 'a request survives a restart'
    after.close()
    assert console_lines(capsys.readouterr().err) == snapshot(
        ['run.aborted run=1 end=daemon_stopped', 'piloop.shutdown Ns']
    )


def test_a_thread_of_a_provider_wakes_its_consumer_in_the_next_turn(tmp_path: Path) -> None:
    daemon = Daemon(Settings(home=tmp_path / 'home'))
    try:
        daemon._wake(BUILDS)
        assert daemon.scheduler.loop._has_woken()
    finally:
        daemon.shutdown(timeout=5)
