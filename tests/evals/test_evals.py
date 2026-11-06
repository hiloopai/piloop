import gzip
import importlib.util
import itertools
import json
import random
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import tomllib
import warnings
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import modal
import pytest
from daytona import Daytona, DaytonaBadRequestError, DaytonaNotFoundError, Sandbox
from daytona_api_client.models.snapshot_state import SnapshotState
from pydantic import JsonValue, SecretStr
from sqlalchemy import select

from evals import daytona, meter, pilot, score as scoring
from evals.drive import (
    CREDENTIALS,
    HALT,
    HEADERS,
    REPO,
    Fleet,
    Piloop,
    Plain,
    admitted,
    agent_box,
    credentials,
    drive,
    export,
    on_team,
    piloop_config,
    prepare,
    price,
    private_files,
    sample,
    sealed_steps,
    watch,
)
from evals.loop import evaluations
from evals.metrics import (
    at,
    piloop_submissions,
    plain_submissions,
    pool,
    pool_counts,
    pool_heads,
    regressions,
    result,
)
from evals.model import (
    ARMS,
    ROOT,
    TASKS,
    Agent,
    Funnel,
    PiloopArm,
    PlainArm,
    Point,
    PoolCounts,
    Regression,
    Regressions,
    Resources,
    Result,
    Sample,
    Settings,
    Task,
    Tokens,
    load_arm,
)
from evals.pilot import guard, spent
from evals.report import plain_bar, write
from evals.sandbox import WORK, Box, Copy, Egress, Run, Step, memory, network_policy
from evals.score import Score, Scoring, score
from evals.team import final_keeps, final_line, notes, races
from evals.telemetry import exported
from piloop.config import CampaignConfig
from piloop.db.models import Campaign
from piloop.db.session import Database
from piloop.deprecated import PiloopDeprecationWarning
from piloop.plugins.campaign_type.kit.candidates.ids import Origin
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.settings import Settings as PiloopSettings
from tests.conftest import baseline


def module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tasks_and_arms_load():
    tasks = [Task.load(p.name) for p in sorted(TASKS.iterdir())]
    assert [t.name for t in tasks] == [p.name for p in sorted(TASKS.iterdir())]
    assert all(t.normalised(t.dev_base) == 0 and t.normalised(t.dev_ref) == 1 for t in tasks)
    arms = {p.stem: load_arm(p.stem) for p in ARMS.glob('*.toml')}
    assert {type(a) for a in arms.values()} == {PiloopArm, PlainArm}


def test_every_piloop_arm_loads_on_every_task_with_this_piloop():
    """An arm's overlay may name keys that this piloop removed: they load with a warning, and the campaign starts."""
    settings = Settings(gateway='https://gw.example', gateway_token=SecretStr('t'))
    arms = [arm for p in sorted(ARMS.glob('*.toml')) if isinstance(arm := load_arm(p.stem), PiloopArm)]
    tasks = [Task.load(p.name) for p in sorted(TASKS.iterdir())]
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', PiloopDeprecationWarning)
        for arm, task in itertools.product(arms, tasks):
            CampaignConfig.model_validate(piloop_config(task, arm, settings, 4))


def test_circles_check_is_exact():
    evaluate = module(TASKS / 't2-circles' / 'repo' / 'evaluate.py')
    packing = module(TASKS / 't2-circles' / 'repo' / 'packing.py')
    circles = [list(c) for c in packing.pack(26)]
    assert evaluate.check(circles, 26) == pytest.approx(Task.load('t2-circles').dev_base)
    touching = [[0.25, 0.5, 0.25], [0.75, 0.5, 0.25]]
    assert evaluate.check(touching, 2) == 0.5
    with pytest.raises(ValueError, match='overlap'):
        evaluate.check([[0.25, 0.5, 0.25], [0.75 - 2**-53, 0.5, 0.25]], 2)
    with pytest.raises(ValueError, match='inside the unit square'):
        evaluate.check([[0.1, 0.5, 0.2]], 1)


def test_enwik8_sealed_counts_every_changed_file(tmp_path: Path):
    sealed = module(TASKS / 't1-enwik8' / 'sealed.py')
    base, tree = tmp_path / 'base', tmp_path / 'tree'
    for d in (base, tree):
        (d / 'data').mkdir(parents=True)
        (d / 'compressor.py').write_text('x' * 10)
        (d / 'evaluate.py').write_text('e')
        (d / 'data' / 'dev.bin').write_bytes(b'd' * 100)
    (tree / 'words.txt').write_text('w' * 7)
    (tree / 'evaluate.py').write_text('changed but ignored')
    assert sealed.diff_bytes(tree, base) == 17


def test_meter_counts_each_response_once(tmp_path: Path):
    claude, codex = tmp_path / 'claude' / 'p', tmp_path / 'codex' / '2026'
    claude.mkdir(parents=True)
    codex.mkdir(parents=True)
    usage = {'input_tokens': 10, 'output_tokens': 5, 'cache_read_input_tokens': 100, 'cache_creation_input_tokens': 1}
    reply = {'type': 'assistant', 'message': {'id': 'm1', 'model': 'claude-opus-5-5', 'usage': usage}}
    (claude / 'a.jsonl').write_text(json.dumps(reply) + '\n' + json.dumps(reply) + '\n{"type": "assist')
    (claude / 'b.jsonl').write_text(json.dumps(reply) + '\n')
    count = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {'total_token_usage': {}}}}
    lines = [{'type': 'turn_context', 'payload': {'model': 'gpt-6-astra'}}]
    for n in (100, 300):
        total = {'input_tokens': n, 'cached_input_tokens': n // 2, 'output_tokens': n // 10}
        lines.append(count | {'payload': {'type': 'token_count', 'info': {'total_token_usage': total}}})
    (codex / 'rollout.jsonl').write_text('\n'.join(map(json.dumps, lines)))
    tokens = meter.main(tmp_path / 'claude', tmp_path / 'codex')
    assert tokens == {
        'claude': {'claude-opus-5-5': {'input': 10, 'output': 5, 'cache_read': 100, 'cache_write': 1, 'turns': 1}},
        'codex': {'gpt-6-astra': {'input': 150, 'output': 30, 'cache_read': 150, 'cache_write': 0, 'turns': 2}},
    }
    assert price(tokens) > 0
    with pytest.raises(ValueError, match='no list price'):
        price({'codex': {'no-such-model': tokens['codex']['gpt-6-astra']}})


def get(value: JsonValue, *keys: str) -> JsonValue:
    for key in keys:
        assert isinstance(value, dict)
        value = value[key]
    return value


def test_piloop_config_puts_agents_on_the_gateway():
    settings = Settings(gateway='https://gw.example', gateway_token=SecretStr('t'))
    arm = PiloopArm(
        kind='piloop',
        name='a',
        piloop='piloop==0.5.0',
        agents=[Agent(harness='codex', model='gpt-6.1-sol'), Agent(harness='claude', model='claude-opus-5-5')],
        overlay={'plugins': {'role': {'implementer': {'max_concurrent': 2}}}},
    )
    config = piloop_config(Task.load('t2-circles'), arm, settings, 4)
    assert get(config, 'plugins', 'stop_rule') == {'max_hours': 4}
    assert get(config, 'plugins', 'role') == {'implementer': {'max_concurrent': 2}}
    assert get(config, 'plugins', 'harness', 'use') == ['codex', 'claude']
    assert get(config, 'plugins', 'harness', 'claude', 'env', 'ANTHROPIC_BASE_URL') == 'https://gw.example'
    assert config['name'] == 't2-circles'


class FakeProcess:
    """A background command that runs until the test ends."""

    stderr = StringIO('')

    def poll(self) -> int | None:
        return None


class FakeBox:
    """A sandbox that records each command, answers each with ``out``, and holds the meter's last count.

    A command that contains a string of ``fail`` exits non-zero, as ``Box.run`` reports it: the first time only when
    ``fail_once``.
    """

    def __init__(
        self, meter: object = None, out: str = '', fail: tuple[str, ...] = (), fail_once: bool = False
    ) -> None:
        self.meter, self.out, self.commands, self.fail, self.fail_once = meter, out, [], fail, fail_once
        self.failed: set[str] = set()
        self.writes: dict[str, bytes] = {}
        self.closed = False
        self.sandbox = SimpleNamespace(object_id='sb-fake')

    def run(self, script: str, timeout: float | None = None) -> str:
        self.commands.append(script)
        for marker in self.fail:
            if marker in script and not (self.fail_once and marker in self.failed):
                self.failed.add(marker)
                raise RuntimeError(f'{script[:40]!r} exited 137: ')
        return self.out

    def start(self, script: str) -> FakeProcess:
        self.commands.append(script)
        return FakeProcess()

    def read(self, path: str) -> bytes:
        if path.endswith('.tgz'):
            buf = BytesIO()
            with tarfile.open(fileobj=buf, mode='w:gz') as tar:
                info = tarfile.TarInfo('out/sessions')
                info.size = 2
                tar.addfile(info, BytesIO(b'1\n'))
            return buf.getvalue()
        return json.dumps(self.meter).encode()

    def write(self, path: str, data: bytes) -> None:
        self.writes[path] = data

    def ended(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


def fleet(box: FakeBox, usd_per_hour: float = 0) -> Fleet:
    return Fleet(
        control=box, agents=lambda: {'sb-fake': box}, usd_per_hour=usd_per_hour, ids=['sb-fake'], close=box.close
    )


METER = {'codex': {'gpt-6.1-sol': {'input': 10, 'output': 5, 'cache_read': 100, 'cache_write': 0, 'turns': 1}}}


def plain_run(settings: Settings) -> Plain:
    arm = PlainArm(
        kind='plain',
        name='p',
        agent=Agent(harness='codex', model='gpt-6.1-sol'),
        resources=Resources(cpu=1, memory_mb=512),
    )
    return Plain(Task.load('t5-lpaq'), arm, settings, 0)


def test_a_failed_spend_sample_is_tried_again(tmp_path: Path):
    settings = Settings(poll_seconds=0)
    box = FakeBox(METER, fail=('meter.py',), fail_once=True)
    run = plain_run(settings)
    stopped = watch(fleet(box), run, 100, 0, settings, tmp_path / 'spend.jsonl', time.monotonic())
    assert stopped == 'hours'
    assert len((tmp_path / 'spend.jsonl').read_text().splitlines()) == 2


def test_a_spend_sample_that_keeps_failing_stops_the_run_and_says_why(tmp_path: Path):
    settings = Settings(poll_seconds=0, sample_attempts=2)
    box = FakeBox(METER, fail=('meter.py',))
    run = plain_run(settings)
    stopped = watch(fleet(box), run, 100, 6, settings, tmp_path / 'spend.jsonl', time.monotonic())
    assert stopped.startswith('sample failed:') and 'exited 137' in stopped
    assert any('pkill' in c for c in box.commands)


def test_a_fatal_error_still_exports_the_run_before_its_sandbox_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box = FakeBox(METER, fail=('env -u EVALS_EVAL_LOG',))
    monkeypatch.setattr('evals.drive.Box.create', lambda *args, **kwargs: box)
    monkeypatch.setattr('evals.drive.image', lambda *args: None)
    settings = Settings(
        gateway='https://gw.example', gateway_token=SecretStr('t'), results=tmp_path, session_ledger=tmp_path / 'ledger'
    )
    arm = plain_run(settings).arm
    with pytest.raises(RuntimeError, match='exited 137'):
        drive(Task.load('t5-lpaq'), arm, 1, 10, 1, settings)
    out = tmp_path / 't5-lpaq' / 'p-1'
    assert any('bundle create' in c for c in box.commands) and box.closed
    assert (out / 'export' / 'out' / 'sessions').exists()
    assert json.loads((out / 'run.json').read_text())['stopped'].startswith('error: RuntimeError')


def test_a_daytona_sandbox_ends_by_itself_after_its_ttl():
    params = daytona.params('snap', {}, public=True, labels={}, offline=False, ttl_minutes=480)
    assert (params.ttl_minutes, params.auto_stop_interval) == (480, 0)


def test_a_daytona_scorer_ends_by_itself_after_its_scoring_could_have_finished(monkeypatch: pytest.MonkeyPatch):
    made: list[object] = []

    class Client:
        def create(self, params: object, timeout: float) -> Sandbox:
            made.append(params)
            return cast(Sandbox, SimpleNamespace(id='sb'))

    monkeypatch.setattr('evals.daytona.snapshot', lambda *args: 'snap')
    task = Task.load('t5-lpaq')
    scoring.scorer_box(task, Settings(score_timeout_minutes=30), cast(Daytona, Client()))
    ttl = cast(daytona.CreateSandboxFromSnapshotParams, made[0]).ttl_minutes
    assert ttl is not None and 30 < ttl <= 120, 'a scorer whose process died still ends'


def test_a_sample_keeps_the_meters_tokens_and_the_sandboxs_compute():
    count = {'input': 10, 'output': 5, 'cache_read': 100, 'cache_write': 1, 'turns': 1}
    box = FakeBox({'claude': {'claude-opus-5-5': count}, 'codex': {'gpt-6.1-sol': count}})
    s = sample({'a': box, 'b': box}, {}, 3.0, 2)
    assert s.tokens == Tokens(input=40, output=20, cache_read=400, cache_write=4, turns=4), 'summed over the boxes'
    assert s.compute_usd == 2 * 3.0 and s.models_usd > 0


def test_a_sample_still_counts_a_box_that_ended_since_the_last():
    box = FakeBox(METER)
    seen: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    sample({'a': box, 'b': box}, seen, 0, 1)
    tokens = sample({'b': box}, seen, 0, 1).tokens
    assert tokens is not None and tokens.input == 20, "a's last count stays"


def identical(n: int) -> PiloopArm:
    return PiloopArm(
        kind='piloop',
        name=f'{n}w',
        piloop='piloop==0.6.0',
        agents=[Agent(harness='codex', model='gpt-6.1-sol')],
        overlay={'plugins': {'role': {'implementer': {'max_concurrent': n}}}},
    )


def test_a_piloop_daemon_reads_its_credentials_from_a_file_and_serves_the_cas_before_init():
    box = FakeBox()
    run = Piloop(Task.load('t2-circles'), identical(2), Settings(), 6)
    run.start(box)
    assert run.served
    serve = next(c for c in box.commands if 'piloop serve --listen 0.0.0.0:8443' in c)
    secret = next(c for c in box.commands if 'piloop secret set GATEWAY_TOKEN' in c)
    assert serve.startswith(f'. {CREDENTIALS} && ') and secret.startswith(f'. {CREDENTIALS} && ')
    order = [next(i for i, c in enumerate(box.commands) if w in c) for w in ('secret set', 'cas serve', 'piloop init')]
    assert order == sorted(order), 'the CAS serves the database that the daemon made, before the baseline waits on it'


def test_a_piloop_run_drains_its_team_before_it_keeps_the_database_and_stops_the_daemon():
    box = FakeBox(out='0')
    Piloop(Task.load('t2-circles'), identical(2), Settings(), 6, served=True).export(box)
    drains = [i for i, c in enumerate(box.commands) if 'piloop limits set' in c]
    assert [box.commands[i].split()[3] for i in drains] == [
        'executor.pools.agents.provisioner.min=0',
        'executor.pools.agents.provisioner.max=0',
        'executor.pools.eval.provisioner.min=0',
        'executor.pools.eval.provisioner.max=0',
    ], 'min first: a max below min is refused'
    backup = next(i for i, c in enumerate(box.commands) if '.backup' in c)
    assert max(drains) < backup and 'piloop stop' in box.commands[backup]
    assert not any('|| true' in box.commands[i] for i in drains), 'a drain that fails says so'


def test_a_team_that_does_not_end_in_time_fails_the_run_once_its_database_is_kept(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr('evals.drive.DRAIN_SECONDS', 0)
    box = FakeBox(out='2')
    with pytest.raises(RuntimeError, match='2 of the team'):
        Piloop(Task.load('t2-circles'), identical(2), Settings(), 6, served=True).export(box)
    assert any('.backup' in c for c in box.commands)


def test_a_run_whose_daemon_never_served_has_no_team_to_drain():
    box = FakeBox(out='0')
    Piloop(Task.load('t2-circles'), identical(2), Settings(), 6).export(box)
    assert not any('provisioner.max' in c for c in box.commands)


def test_spend_between_samples():
    spend = [Sample(hours=0, models_usd=0, compute_usd=0), Sample(hours=1, models_usd=8, compute_usd=2)]
    assert [at(spend, h) for h in (0, 0.5, 2)] == [0, 5, 10]


def git(repo: Path, *args: str, when: datetime | None = None) -> str:
    env = {'GIT_COMMITTER_DATE': when.isoformat(), 'GIT_AUTHOR_DATE': when.isoformat()} if when else {}
    env |= {'GIT_AUTHOR_NAME': 'a', 'GIT_AUTHOR_EMAIL': 'a@a', 'GIT_COMMITTER_NAME': 'a', 'GIT_COMMITTER_EMAIL': 'a@a'}
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout


def test_plain_result_and_report(tmp_path: Path):
    started = datetime(2026, 10, 30, tzinfo=UTC)
    repo, run = tmp_path / 'repo', tmp_path / 'run'
    out = run / 'export' / 'out'
    out.mkdir(parents=True)
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    revs = []
    for minutes in (0, 30, 90, 150):
        (repo / 'f').write_text(str(minutes))
        git(repo, 'add', 'f')
        git(repo, 'commit', '-qm', str(minutes), when=started + timedelta(minutes=minutes))
        revs.append(git(repo, 'rev-parse', 'HEAD').strip())
    git(repo, 'bundle', 'create', str(out / 'repo.bundle'), '--all')
    (out / 'sessions').write_text('2\n')
    meta = {'task': 't2-circles', 'arm': 'plain-codex', 'repeat': 1, 'piloop': None, 'base': revs[0]}
    meta |= {'started_at': started.isoformat(), 'budget_usd': 20, 'max_hours': 4, 'stopped': 'budget'}
    (run / 'run.json').write_text(json.dumps(meta))
    spend = [
        Sample(hours=h, models_usd=10 * h, compute_usd=0, tokens=Tokens(input=100 * h, output=10 * h, turns=h))
        for h in (0, 1, 2, 3)
    ]
    (run / 'spend.jsonl').write_text('\n'.join(s.model_dump_json() for s in spend))
    scores = {rev: Score(dev=i / 3, sealed=i / 4, detail='') for i, rev in enumerate(revs)}
    r = result(run, scores)
    assert [(p.revision, p.usd) for p in r.curve] == [(revs[0], 0), (revs[1], 5), (revs[2], 15)]
    assert [p.models_usd for p in r.curve] == [0, 5, 15]
    assert [p.tokens for p in r.curve] == [Tokens(), Tokens(input=50, output=5), Tokens(input=150, output=15, turns=1)]
    assert r.funnel == Funnel(ideas=None, runs=2, submissions=3, kept=3)
    assert r.first_keep is not None and r.first_keep.usd == 5
    assert r.gap == pytest.approx(2 / 3 - 2 / 4)
    other = r.model_copy(
        update={'arm': 'plain-claude', 'curve': [Point(revision='b', hours=0, usd=0, dev=0, sealed=None)]}
    )
    [page] = write([r, other], tmp_path / 'site', Settings())
    text = page.read_text()
    assert page.name == 't2-circles.html' and 'plain-claude' in text and 'plotly' in text and 'regression share' in text
    assert r.regressions == Regressions(submissions=3, scored=3, found=None), 'every commit on main, scored as a head'
    by_arm = {'plain-codex': [r], 'plain-claude': [other]}
    assert plain_bar(by_arm) == pytest.approx(2 / 4)
    assert plain_bar(by_arm, frozenset({'plain-codex'})) == 0
    assert isinstance(Result.model_validate_json(r.model_dump_json()), Result)


def test_piloop_arms_export_to_the_same_collector(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv('OTEL_EXPORTER_OTLP_ENDPOINT', raising=False)
    assert exported(Settings(), 'r') == {}
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_ENDPOINT', 'https://otel.example')
    monkeypatch.setenv('OTEL_EXPORTER_OTLP_HEADERS', 'authorization=Bearer t')
    monkeypatch.setenv('OTEL_RESOURCE_ATTRIBUTES', 'team=x')
    assert exported(Settings(), 't1/a-1') == {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://otel.example',
        'OTEL_EXPORTER_OTLP_HEADERS': 'authorization=Bearer t',
        'OTEL_RESOURCE_ATTRIBUTES': 'team=x,evals.run=t1/a-1',
    }


def test_a_sealed_split_arm_gets_dev_and_its_own_sealed_command():
    v0 = load_arm('piloop-v0')
    assert isinstance(v0, PiloopArm) and v0.split == 'sealed'
    config = piloop_config(Task.load('t1-enwik8'), v0, Settings(gateway='https://gw.example'), 4)
    nice = ['nice', '-n', '10']
    assert get(config, 'evaluator', 'dev') == [*nice, 'uv', 'run', 'python', 'evaluate.py']
    assert get(config, 'evaluate') == [*nice, 'uv', 'run', 'python', 'evaluate.py']
    sealed = get(config, 'evaluator', 'sealed')
    assert isinstance(sealed, list) and sealed[:5] == [*nice, 'python3', '/opt/evals/campaign_sealed.py']
    assert get(config, 'evaluator', 'scoring') == 'submit'


def test_campaign_sealed_scores_the_checkout_on_the_split(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    campaign_sealed = module(ROOT / 'campaign_sealed.py')
    checkout, sealed = tmp_path / 'checkout', tmp_path / 'sealed'
    shutil.copytree(TASKS / 't2-circles' / 'repo', checkout)
    sealed.mkdir()
    (sealed / 'meta.json').write_text(json.dumps({'n': [4], 'base': {'4': 1.0}, 'ref': {'4': 2.0}}))
    monkeypatch.chdir(checkout)
    result = campaign_sealed.main(TASKS / 't2-circles', sealed, 'higher')
    assert result['status'] == 'ok' and result['direction'] == 'higher'
    assert result['score'] == pytest.approx(0, abs=1e-6)
    (checkout / 'packing.py').write_text('def pack(n):\n    return [(0.5, 0.5, 0.6)] * n\n')
    assert campaign_sealed.main(TASKS / 't2-circles', sealed, 'higher')['status'] == 'crash'
    assert campaign_sealed.main(TASKS / 't2-circles', sealed, 'lower')['status'] == 'crash'


def test_the_guard_halts_every_unfinished_run_at_the_ceiling(tmp_path: Path):
    settings = Settings(results=tmp_path, poll_seconds=0.01)
    for name, usd, finished in (('a-1', 400, True), ('b-1', 700, False)):
        run = tmp_path / 't' / name
        run.mkdir(parents=True)
        (run / 'spend.jsonl').write_text(Sample(hours=1, models_usd=usd, compute_usd=0).model_dump_json())
        if finished:
            (run / 'run.json').write_text('{}')
    assert spent(settings) == 1100
    done = threading.Event()
    watcher = threading.Thread(target=guard, args=(settings, 1000, done))
    watcher.start()
    halt = tmp_path / 't' / 'b-1' / HALT
    try:
        for _ in range(500):
            if halt.exists():
                break
            time.sleep(0.01)
    finally:
        done.set()
        watcher.join()
    assert 'ceiling of $1000' in halt.read_text()
    assert not (tmp_path / 't' / 'a-1' / HALT).exists()


def test_team_counts_keeps_in_the_final_head_races_and_notes_read_across(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    revs = []
    for name in ('base', 'a1', 'b-on-a1'):
        (repo / 'f').write_text(name)
        git(repo, 'add', 'f')
        git(repo, 'commit', '-qm', name)
        revs.append(git(repo, 'rev-parse', 'HEAD').strip())
    git(repo, 'checkout', '-q', '-b', 'side', revs[0])
    (repo / 'g').write_text('a2')
    git(repo, 'add', 'g')
    git(repo, 'commit', '-qm', 'a2 not on b')
    revs.append(git(repo, 'rev-parse', 'HEAD').strip())
    bundle = tmp_path / 'repo.bundle'
    git(repo, 'bundle', 'create', str(bundle), '--all')
    with closing(sqlite3.connect(':memory:')) as db:
        db.executescript(
            """
            create table runs (id integer, role text);
            create table candidates (id integer, revision text, run_id integer, verdict text, updated_at text);
            create table events (target_kind text, target_id integer, kind text, at text, payload text);
            create table agent_sessions (id integer, kind text, subject_kind text, subject_id integer, session_id text);
            create table comments (author text, body text, kind text);
            insert into runs values (1, 'implementer'), (2, 'implementer_b'), (3, 'implementer');
            insert into agent_sessions values (10, 'implementer', 'run', 1, 's1'), (20, 'implementer_b', 'run', 2, 's2');
            insert into comments values ('implementer#10', 'The   mixer weights saturate at 4096.', 'note'),
                ('implementer_b#20', 'Never read by anyone.', 'note'), ('human', 'Not a worker.', 'note');
            """
        )
        for i, (rev, run) in enumerate(zip(revs, (None, 1, 2, 3), strict=True)):
            db.execute("insert into candidates values (?, ?, ?, 'keep', ?)", (i, rev, run, f'2026-01-01 00:0{i}'))
        assert final_keeps(db, bundle) == (
            {'implementer': 2, 'implementer_b': 1},
            {'implementer': 1, 'implementer_b': 0},
        )
        for i, (verdict, parent, best) in enumerate(
            (('discard', 5, -1), ('discard', -2, -3), ('keep', 4, 4)), start=10
        ):
            db.execute("insert into candidates values (?, 'x', 1, ?, '2026-01-02')", (i, verdict))
            judged = json.dumps({'parent_delta': parent, 'best_delta': best})
            db.execute("insert into events values ('candidate', ?, 'candidate.judged', '2026-01-02', ?)", (i, judged))
        assert races(db) == 1
        sessions = tmp_path / 'export' / '.claude' / 'projects' / 'p'
        sessions.mkdir(parents=True)
        read = {
            'type': 'user',
            'message': {
                'content': [{'type': 'tool_result', 'content': 'notes: The mixer weights\n saturate at 4096. More.'}]
            },
        }
        (sessions / 's2.jsonl').write_text(json.dumps(read) + '\n')
        assert notes(db, tmp_path / 'export') == (
            {'implementer': 1, 'implementer_b': 1},
            {'implementer>implementer_b': 1},
        )


def test_a_score_is_the_mean_of_its_scorings_and_keeps_each_cpu():
    amd = Scoring(dev=1.0, sealed=0.8, detail='ok', cpu='AuthenticAMD 25/17')
    intel = Scoring(dev=0.8, sealed=0.4, detail='ok', cpu='GenuineIntel 6/106')
    both = Score.of([amd, intel])
    assert (both.dev, both.sealed) == (pytest.approx(0.9), pytest.approx(0.6))
    assert 'AuthenticAMD 25/17' in both.detail and 'GenuineIntel 6/106' in both.detail
    assert Score.of([amd, intel.model_copy(update={'sealed': None})]).sealed is None


def test_t4_mixed_names_share_long_starts_and_ends():
    gen = module(TASKS / 't4-1brc' / 'build' / 'gen.py')
    names = gen.names(random.Random(1), 300, 'mixed')
    assert len(names) == 300 and all(0 < len(n) <= 100 for n in names)
    assert sum(n.isascii() for n in names) >= 100
    shared = Counter((n[:32], n[-16:]) for n in names if len(n) > 48)
    assert shared.most_common(1)[0][1] >= 5


def test_t5_holdout_fails_a_round_trip_over_its_cpu_rule(tmp_path: Path):
    sealed = module(TASKS / 't5-lpaq' / 'sealed.py')
    (tmp_path / 'evaluate.py').write_text(
        'def score(path, tree):\n    return {"bytes": 90, "seconds": float(path.read_text())}\n'
    )
    (tmp_path / 'meta.json').write_text(json.dumps({'base': 100, 'ref': 80}))
    (tmp_path / 'holdout.bin').write_text('127')
    assert sealed.main(tmp_path, tmp_path, tmp_path)['status'] == 'over_limit'
    (tmp_path / 'holdout.bin').write_text('125')  # within the 5% band over 120 s
    assert sealed.main(tmp_path, tmp_path, tmp_path)['score'] == pytest.approx(0.5)


def fake_codec(root: Path) -> Path:
    """A codec that copies its input: no CPU of its own."""
    codec = root / 'build' / 'codec'
    codec.parent.mkdir(parents=True, exist_ok=True)
    codec.write_text('#!/bin/sh\ncp "$2" "$3"\n')
    codec.chmod(0o755)
    return codec


def test_t5_counts_the_codecs_cpu_not_its_build_and_scales_it_by_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    evaluate = module(TASKS / 't5-lpaq' / 'repo' / 'evaluate.py')

    def build(root: Path) -> Path:
        subprocess.run(
            [sys.executable, '-c', 'import time\nt = time.process_time()\nwhile time.process_time() - t < 0.5: pass'],
            check=True,
        )
        return fake_codec(root)

    monkeypatch.setattr(evaluate, 'build', build)
    monkeypatch.setattr(evaluate, 'reference_seconds', lambda data: 2 * evaluate.CANONICAL)
    (tmp_path / 'data.bin').write_bytes(b'x' * 1000)
    metrics = evaluate.score(tmp_path / 'data.bin', tmp_path)
    assert metrics['raw_seconds'] < 0.3
    assert metrics['ref_seconds'] == 2 * evaluate.CANONICAL
    assert metrics['seconds'] == pytest.approx(metrics['raw_seconds'] / 2, abs=0.01)


def test_t5_caps_the_codecs_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evaluate = module(TASKS / 't5-lpaq' / 'repo' / 'evaluate.py')
    monkeypatch.setattr(evaluate, 'MEMORY', 512 << 20)
    (tmp_path / 'data.bin').write_bytes(b'x' * 1000)
    for mib, fails in ((16, False), (1024, True)):
        codec = tmp_path / f'codec{mib}'
        codec.write_text(
            f'#!{sys.executable}\nimport shutil, sys\nb = bytearray({mib} << 20)\nshutil.copy(sys.argv[2], sys.argv[3])\n'
        )
        codec.chmod(0o755)
        if fails:
            with pytest.raises(RuntimeError, match='8 GiB'):
                evaluate.run(codec, tmp_path / 'data.bin', tmp_path)
        else:
            assert evaluate.run(codec, tmp_path / 'data.bin', tmp_path) >= 0


def test_t5_times_its_reference_once_per_box_until_it_is_old(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    evaluate = module(TASKS / 't5-lpaq' / 'repo' / 'evaluate.py')
    timed: list[float] = []
    now = [1000.0]
    monkeypatch.setattr(evaluate, 'CACHE', tmp_path / 'cache')
    monkeypatch.setattr(evaluate.time, 'time', lambda: now[0])
    monkeypatch.setattr(evaluate, 'time_reference', lambda data: timed.append(now[0]) or 18.0)
    (tmp_path / 'data.bin').write_bytes(b'x' * 1000)
    assert evaluate.reference_seconds(tmp_path / 'data.bin') == 18.0
    now[0] += evaluate.REFRESH - 1
    evaluate.reference_seconds(tmp_path / 'data.bin')
    now[0] += 2
    evaluate.reference_seconds(tmp_path / 'data.bin')
    assert timed == [1000.0, 1000.0 + evaluate.REFRESH + 1]


AVX512 = """#include <immintrin.h>
__attribute__((target("avx512f"))) void add(int *a, const int *b) {
    _mm512_storeu_si512(a, _mm512_add_epi32(_mm512_loadu_si512(a), _mm512_loadu_si512(b)));
}
int main(int argc, char **argv) { int a[16] = {0}, b[16] = {1}; if (argc > 5) add(a, b); return a[0]; }
"""
AVX2 = """#include <immintrin.h>
__attribute__((target("avx2"))) void add(int *a, const int *b) {
    _mm256_storeu_si256((__m256i *)a, _mm256_add_epi32(_mm256_loadu_si256((__m256i *)a), _mm256_loadu_si256((__m256i *)b)));
}
int main(int argc, char **argv) { int a[8] = {0}, b[8] = {1}; if (argc > 5) add(a, b); return a[0]; }
"""


@pytest.mark.skipif(shutil.which('g++') is None or shutil.which('objdump') is None, reason='needs g++ and objdump')
def test_t5_refuses_a_codec_with_avx512_instructions(tmp_path: Path):
    evaluate = module(TASKS / 't5-lpaq' / 'repo' / 'evaluate.py')
    for name, source in (('v4', AVX512), ('v3', AVX2)):
        (tmp_path / name).mkdir()
        (tmp_path / name / 'codec.cpp').write_text(source)
    with pytest.raises(RuntimeError, match='AVX-512'):
        evaluate.build(tmp_path / 'v4')
    assert evaluate.build(tmp_path / 'v3').exists()


def test_the_scorer_records_the_cpus_avx512_support(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    scorer = module(ROOT / 'scorer.py')
    flags = 'vendor_id\t: AuthenticAMD\ncpu family\t: 25\nmodel\t\t: 17\nflags\t\t: fpu avx2 {}\n'
    for extra, label in (('avx512f avx512vnni', 'AuthenticAMD 25/17 avx512'), ('', 'AuthenticAMD 25/17 no-avx512')):
        (tmp_path / 'cpuinfo').write_text(flags.format(extra))
        monkeypatch.setattr(scorer, 'CPUINFO', tmp_path / 'cpuinfo')
        assert scorer.cpu() == label
    (tmp_path / 'cpuinfo').write_text(flags.format('avx512f') + 'model name\t: AMD EPYC 9254 24-Core Processor\n')
    assert scorer.cpu() == 'AuthenticAMD 25/17 avx512 (AMD EPYC 9254 24-Core Processor)'


def test_the_wrapper_logs_each_evaluation_and_who_ran_it(tmp_path: Path):
    log = tmp_path / 'evaluations.jsonl'
    (tmp_path / 'evaluate.py').write_text('print(1)\n')
    env = {'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT / 'wrap'), 'EVALS_EVAL_LOG': str(log)}
    for extra, args in (({}, []), ({'PILOOP_RESULT_FILE': 'r'}, []), ({}, ['--serve', 'x'])):
        subprocess.run([sys.executable, 'evaluate.py', *args], cwd=tmp_path, env=env | extra, check=True)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(line['argv'], line['piloop']) for line in lines] == [([], False), ([], True)]
    assert len(evaluations(log)) == 1


def test_the_plain_arm_resumes_its_session_in_the_sandbox_without_waiting():
    arm = load_arm('plain-claude')
    assert isinstance(arm, PlainArm)
    plain = Plain(Task.load('t5-lpaq'), arm, Settings(), 4)
    script = plain.script('go')
    assert '--session-id $session' in script and 'while true' in script and '--resume $session' in script


def test_a_result_from_an_earlier_round_loads_without_its_removed_fields():
    started = datetime(2026, 11, 1, tzinfo=UTC)
    r = Result(task='t', arm='a', repeat=1, piloop='p', started_at=started, budget_usd=60, max_hours=4, stopped='hours',
               spend=[], curve=[Point(revision='b', hours=0, usd=0, dev=0, sealed=0)], funnel=Funnel(ideas=1, runs=1, submissions=1, kept=0),
               escalations=0, interventions=0, lead_token_share=None)  # fmt: skip
    old = json.loads(r.model_dump_json()) | {'built_on': {'a>b': 1}, 'replaced': {}}
    assert Result.model_validate(old) == r
    assert r.curve[0].tokens is None and Sample(hours=0, models_usd=0, compute_usd=0).tokens is None
    with pytest.raises(ValueError, match='extra'):
        Result.model_validate(old | {'unknown': 1})


def test_a_gateway_only_sandbox_reaches_the_gateways_host_alone():
    settings = Settings(gateway='https://gateway.example.com/route')
    assert network_policy(settings, 'gateway') == Egress(
        block_network=False, outbound_domain_allowlist=['gateway.example.com']
    )
    assert network_policy(settings, 'open') == Egress(block_network=False)
    assert network_policy(settings, 'none') == Egress(block_network=True)
    with pytest.raises(ValueError, match='EVALS_GATEWAY'):
        network_policy(Settings(gateway=''), 'gateway')


def test_a_tasks_memory_limit_is_reserved_and_capped():
    assert memory(4096, None) == 4096
    assert memory(4096, 12288) == (12288, 12288)
    assert memory(24576, 12288) == (24576, 24576)


def test_only_t6_locks_egress_caps_memory_and_has_a_private_split():
    t6 = Task.load('t6-optcomp')
    assert (t6.network, t6.memory_limit_mb, t6.private) == ('gateway', 12288, True)
    for name in ('t3-airbench', 't4-1brc', 't5-lpaq'):
        task = Task.load(name)
        assert (task.network, task.memory_limit_mb, task.private) == ('open', None, False)


def test_a_private_split_lands_in_the_base_commit():
    box = FakeBox(out='abc\n')
    assert prepare(box, Task.load('t6-optcomp'), private=b'split') == 'abc'
    assert box.writes == {f'{WORK}/private.tgz': b'split'}
    script = box.commands[-1]
    assert script.index(f'tar xzf {WORK}/private.tgz -C {REPO}') < script.index('git add -A')


def test_a_private_task_needs_its_split_and_others_fetch_none(monkeypatch: pytest.MonkeyPatch):
    import evals.drive

    def tar(*names: str) -> bytes:
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as t:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 1
                t.addfile(info, BytesIO(b'x'))
        return buf.getvalue()

    fetched: list[str] = []
    monkeypatch.setattr(evals.drive, 'sealed_data', lambda name, settings: fetched.append(name) or tar())
    with pytest.raises(ValueError, match='t6-optcomp.private'):
        private_files(Task.load('t6-optcomp'), Settings())
    monkeypatch.setattr(evals.drive, 'sealed_data', lambda name, settings: fetched.append(name) or tar('a.lean'))
    assert private_files(Task.load('t6-optcomp'), Settings()) == tar('a.lean')
    assert private_files(Task.load('t5-lpaq'), Settings()) is None
    assert fetched == ['t6-optcomp.private', 't6-optcomp.private']


def test_t6_refuses_code_that_runs_or_skips_the_kernel_but_not_a_comment_naming_it():
    evaluate = module(TASKS / 't6-optcomp' / 'repo' / 'evaluate.py')
    for bad in (
        'by native_decide',
        'axiom cheat : False',
        'set_option debug.skipKernelTC true in',
        '#eval IO.println 1',
    ):
        assert evaluate.BANNED.search(evaluate.code_of(f'theorem t : True := by\n  trivial\n{bad}\n'))
    assert not evaluate.BANNED.search(
        evaluate.code_of('/-- In general it is unsafe: no `native_decide`. -/\n-- #eval\n')
    )
    assert evaluate.BANNED.search('elab "go" : tactic => pure ()\n')
    assert evaluate.BANNED.search('  scoped elab_rules : tactic | `(tactic| go) => pure ()\n')
    assert not evaluate.BANNED.search('theorem t : («elab» h).x = y := by simp [«elab», h.elab_eq]\n')
    assert evaluate.BANNED.search('@[tactic go] def goImpl : Lean.Elab.Tactic.Tactic := fun _ => pure ()\n')
    assert not evaluate.BANNED.search(
        'def Environment := String → Int\ntheorem e : (fun _ => 0 : Environment) = 0 := rfl\n'
    )
    for runs_code in (
        'attribute [command_elab Lean.Parser.Command.foo] f\n',
        'attribute [local tactic Lean.Parser.Tactic.foo] g\n',
        'private initialize x : IO.Ref Nat ← IO.mkRef 0\n',
        '@[delab app.foo] def delabFoo : Lean.PrettyPrinter.Delaborator.Delab := failure\n',
        '@[builtin_command_elab foo] def f : Lean.Elab.Command.CommandElab := fun _ => pure ()\n',
        'attribute [csimp] fast_eq\n',
        'register_option foo.bar : Bool := { defValue := false }\n',
        'private axiom cheat : False\n',
        'scoped simproc reduceFoo (foo _) := fun _ => pure .continue\n',
    ):
        assert evaluate.BANNED.search(runs_code), runs_code
    for runs_nothing in (
        'attribute [simp] foo_bar\n',
        '@[simp, grind] theorem tactic_free : True := trivial\n',
        'macro "go" : tactic => `(tactic| simp)\n',
    ):
        assert not evaluate.BANNED.search(runs_nothing), runs_nothing


def test_t6_counts_a_private_split_in_proved_and_alone(tmp_path: Path):
    evaluate = module(TASKS / 't6-optcomp' / 'repo' / 'evaluate.py')
    axioms = ['Classical.choice', 'Quot.sound', 'propext']
    public = {
        'targets': ['a', 'b'],
        'protected': {'M': {'a': 'theorem 1', 'b': 'theorem 2'}},
        'standard_axioms': axioms,
    }
    (tmp_path / 'spec.json').write_text(json.dumps(public))
    assert evaluate.load_spec(tmp_path)['splits'] == {'public': ['a', 'b']}
    private = {'split': 'private', 'targets': ['c'], 'protected': {'P': {'c': 'theorem 3'}}, 'standard_axioms': axioms}
    (tmp_path / 'spec.private.json').write_text(json.dumps(private))
    spec = evaluate.load_spec(tmp_path)
    assert spec['targets'] == ['a', 'b', 'c']
    assert spec['splits'] == {'public': ['a', 'b'], 'private': ['c']}
    assert spec['modules_of'] == {'a': 'M', 'b': 'M', 'c': 'P'}
    assert spec['protected']['c'] == {'kind': 'theorem', 'hash': '3'}


class FakeControl(FakeBox):
    """The control sandbox of a team: it has a public URL for each port, and an id."""

    id = 'sb-control'

    def preview(self, port: int) -> str:
        return f'https://{port}-sb.example'


def team(
    monkeypatch: pytest.MonkeyPatch, task: str, arm: PiloopArm, evaluators_per_worker: float = 1.0
) -> tuple[Fleet, FakeControl, list[list[Step]], dict[str, dict[str, str]]]:
    """A team of ``arm`` on ``task`` with no platform: what it built, and the tags that its meter lists by."""
    control, listed = FakeControl(), {}
    made: list[list[Step]] = []
    monkeypatch.setattr('evals.drive.daytona.client', lambda settings: 'daytona')
    monkeypatch.setattr('evals.drive.daytona.control', lambda *args: control)
    monkeypatch.setattr('evals.drive.daytona.snapshot', lambda cloud, steps, size: made.append(steps) or 'snap-1')
    monkeypatch.setattr('evals.drive.built', lambda settings, steps: made.append(steps) or f'im-{len(made)}')
    monkeypatch.setattr('evals.drive.sealed_data', lambda name, settings: tgz({'split.json': b'{}'}))
    monkeypatch.setattr('evals.drive.modal_config', {'token_id': 'ak-1', 'token_secret': 'as-1'})
    monkeypatch.setattr('evals.drive.tagged', lambda settings, tags: listed.update(tags) or {'sb-w': FakeBox(METER)})
    settings = Settings(
        gateway='https://gw.example',
        gateway_token=SecretStr('gw-secret'),
        daytona_api_key=SecretStr('dtn-secret'),
        evaluators_per_worker=evaluators_per_worker,
    ).model_copy(update={'otlp_headers': SecretStr('authorization=otlp-secret')})
    env = {
        'OTEL_EXPORTER_OTLP_ENDPOINT': 'https://otel.example',
        'OTEL_EXPORTER_OTLP_HEADERS': 'authorization=otlp-secret',
        'EVALS_EVAL_LOG': '/work/out/e.jsonl',
    }
    found, run = on_team(Task.load(task), arm, settings, 6, env, f'{task}/{arm.name}-1')
    assert isinstance(run, Piloop)
    found.agents()
    return found, control, made, {'tags': listed}


def tgz(files: dict[str, bytes]) -> bytes:
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    return buf.getvalue()


def test_a_piloop_arms_team_is_its_daemons_own_pools_on_modal_and_daytona(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    arm = identical(2).model_copy(update={'split': 'sealed'})
    found, control, made, listed = team(monkeypatch, 't5-lpaq', arm, evaluators_per_worker=1.5)
    (tmp_path / 'config.toml').write_bytes(control.writes['/root/.piloop-evals/config.toml'])
    assert PiloopSettings(home=tmp_path).providers is not None, 'a valid home, its provisioners included'
    pools = tomllib.loads((tmp_path / 'config.toml').read_text())['plugins']['executor']['pools']
    assert pools['serve'] == {'agents': 0, 'slots': {'evaluation': 0}}
    agents, evaluate = pools['agents']['provisioner'], pools['eval']['provisioner']
    assert (agents['min'], agents['max'], evaluate['min'], evaluate['max']) == (2, 2, 3, 3), 'the whole team at once'
    assert agents['compute'] | {'env': None} == {
        'provider': 'modal',
        'app': 'piloop-evals',
        'image_id': 'im-1',
        'cpu': 2,
        'memory_mb': 8192,
        'cloud': 'gcp',
        'timeout_hours': 7,
        'env': None,
    }, 'an 8 GiB request with no hard limit: a box over a hard one dies with every session in it'
    assert evaluate['compute'] | {'env': None} == {
        'provider': 'daytona',
        'snapshot': 'snap-1',
        'ttl_hours': 7,
        'env': None,
    }, 't5 is scored on Daytona'
    assert [type(s).__name__ for s in made[1][len(made[0]) :]] == ['Copy'], "the sealed split: the evaluators' only"
    sealed = made[1][-1]
    assert isinstance(sealed, Copy) and sealed.remote == '/root/.piloop-evals/sealed'
    env = agents['compute']['env']
    assert json.loads(env['PILOOP_PLUGINS'])['cas']['remote'] == {'provider': 'http', 'url': 'https://7701-sb.example'}
    assert not any(k.startswith('OTEL_') for k in env) and env['EVALS_EVAL_LOG'] == '/work/out/e.jsonl'
    secrets = control.writes['/root/.piloop-evals/credentials.env'].decode()
    assert all(f'{k}=' in secrets for k in ('GATEWAY_TOKEN', 'MODAL_TOKEN_SECRET', 'DAYTONA_API_KEY', HEADERS))
    assert 'secret' not in control.writes['/root/.piloop-evals/config.toml'].decode(), 'no key in the config'
    assert control.commands[0] == 'mkdir -p -m 700 /root/.piloop-evals', (
        'the daemon serves its socket in a private home'
    )
    owner = control.writes['/root/.piloop-evals/owner'].decode()
    assert listed['tags'] == {'piloop-owner': owner, 'piloop-pool': 'agents'}, "the meter finds the daemon's workers"
    assert found.ids == ['sb-control'] and found.described['evaluators_on'] == 'daytona'


def test_a_team_on_a_gateway_only_task_reaches_the_gateway_and_its_control_sandbox_and_is_scored_on_modal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    found, control, _, _ = team(monkeypatch, 't6-optcomp', identical(1))
    pools = tomllib.loads(control.writes['/root/.piloop-evals/config.toml'].decode())['plugins']['executor']['pools']
    agents, evaluate = pools['agents']['provisioner']['compute'], pools['eval']['provisioner']['compute']
    hosts = ['8443-sb.example', '7701-sb.example']
    assert agents['outbound_domains'] == ['gw.example', *hosts] and agents['memory_limit_mb'] == 12288
    assert (evaluate['provider'], evaluate['outbound_domains'], evaluate['memory_limit_mb']) == ('modal', hosts, 12288)
    assert found.described['evaluators_on'] == 'modal'


def test_the_same_sealed_split_gives_the_evaluators_the_same_image(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = gzip.decompress(tgz({'split.json': b'{}'}))
    first, later = gzip.compress(raw, mtime=1), gzip.compress(raw, mtime=2)  # as sealed_data makes it, a second apart
    assert first != later
    splits = iter([first, later])
    monkeypatch.setattr('evals.drive.sealed_data', lambda name, settings: next(splits))
    arm = identical(1).model_copy(update={'split': 'sealed'})
    task = Task.load('t5-lpaq')
    assert sealed_steps(task, arm, Settings()) == sealed_steps(task, arm, Settings())


def test_a_team_that_cannot_start_deletes_its_control_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('evals.drive.credentials', lambda settings: (_ for _ in ()).throw(ValueError('no Modal token')))
    with pytest.raises(ValueError, match='Modal'):
        team(monkeypatch, 't5-lpaq', identical(1))


def test_the_credentials_name_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr('evals.drive.modal_config', {})
    with pytest.raises(ValueError, match='MODAL_TOKEN_ID, MODAL_TOKEN_SECRET, DAYTONA_API_KEY'):
        credentials(Settings(gateway_token=SecretStr('t')))


def test_a_team_drains_even_when_an_agents_sandbox_cannot_give_its_sessions(tmp_path: Path):
    control, worker = FakeBox(METER, out='0'), FakeBox(METER, fail=('sessions.tgz',))
    good = FakeBox(METER)
    agents = {'sb-w': worker, 'sb-g': good}
    team_ = Fleet(control=control, agents=lambda: agents, usd_per_hour=0, ids=['a'], close=control.close)
    failed = export(team_, Piloop(Task.load('t2-circles'), identical(2), Settings(), 6, served=True), tmp_path)
    assert [f.split(':')[0] for f in failed] == ['workers/sb-w'] and 'exited 137' in failed[0]
    assert any('provisioner.max=0' in c for c in control.commands) and any('piloop stop' in c for c in control.commands)
    assert (tmp_path / 'export' / 'workers' / 'sb-g').is_dir() and (tmp_path / 'export' / 'out').is_dir(), 'the rest'


def test_a_provisioned_sandbox_gets_the_driver_s_work_directory_before_the_meter_writes_there():
    box = FakeBox(METER)
    sample({'sb-w': box}, {}, 0, 1)
    assert box.commands[0].startswith(f'mkdir -p {WORK} && ')


def test_the_export_takes_each_agents_sessions_before_the_run_drains(tmp_path: Path):
    control, worker = FakeBox(METER), FakeBox(METER)
    team_ = Fleet(
        control=control,
        agents=lambda: {'sb-c': control, 'sb-w': worker},
        usd_per_hour=0,
        ids=['a'],
        close=control.close,
    )
    export(team_, plain_run(Settings()), tmp_path)
    assert (tmp_path / 'export' / 'out' / 'sessions').exists() and (tmp_path / 'export.tgz').exists()
    assert (tmp_path / 'export' / 'workers' / 'sb-w').is_dir() and not (
        tmp_path / 'export' / 'workers' / 'sb-c'
    ).exists()
    assert ' out' not in worker.commands[0], "a worker's logs stay behind"


def test_a_snapshots_name_changes_with_the_files_it_copies(tmp_path: Path):
    (tmp_path / 'f').write_text('a')
    steps = [Run(('true',)), Copy(tmp_path / 'f', '/opt/f')]
    first = daytona.fingerprint(steps)
    assert daytona.fingerprint(steps) == first
    (tmp_path / 'f').write_text('b')
    assert daytona.fingerprint(steps) != first


class Snapshots:
    """Daytona's snapshots, where another caller's build of the one asked for goes through ``states``, if any.

    That build starts just before ours, so our create gets the quota error that its pending build causes. A sleep moves
    the build to its next state.
    """

    def __init__(self, *states: SnapshotState) -> None:
        self.states, self.started = list(states), False

    def get(self, name: str) -> SimpleNamespace:
        if not (self.started and self.states):
            raise DaytonaNotFoundError(f'{name} not found')
        return SimpleNamespace(state=self.states[0], error_reason=None)

    def create(self, params: object, timeout: float) -> None:
        self.started = True
        raise DaytonaBadRequestError('Snapshot quota exceeded. Maximum allowed: 30', status_code=400)


@pytest.mark.parametrize('other', [True, False])
def test_a_snapshot_create_that_fails_waits_for_another_callers_build_and_else_raises_its_error(
    other: bool, monkeypatch: pytest.MonkeyPatch
):
    snapshots = Snapshots(SnapshotState.PENDING, SnapshotState.BUILDING, SnapshotState.ACTIVE) if other else Snapshots()
    monkeypatch.setattr('evals.daytona.time.sleep', lambda seconds: snapshots.states.pop(0))
    cloud = cast(Daytona, SimpleNamespace(snapshot=snapshots))
    if other:
        assert daytona.snapshot(cloud, [Run(('true',))], daytona.EVALUATOR).endswith('-1c8g')
        assert snapshots.states == [SnapshotState.ACTIVE], 'it waited for the build to end'
    else:
        with pytest.raises(DaytonaBadRequestError, match='quota exceeded'):
            daytona.snapshot(cloud, [Run(('true',))], daytona.EVALUATOR)


def test_the_driver_refuses_a_run_that_would_take_the_gateways_sessions_over_the_cap(tmp_path: Path):
    settings = Settings(max_sessions=20, session_ledger=tmp_path / 'ledger')
    (tmp_path / 'ledger').mkdir()
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    (tmp_path / 'ledger' / 'lean.json').write_text(json.dumps({'run': 'lean pilot', 'sessions': 3, 'until': later}))
    (tmp_path / 'ledger' / 'gone.json').write_text(json.dumps({'run': 'ended', 'sessions': 9, 'pid': 2**22 + 7}))
    with admitted(settings, 'a', 10):
        with pytest.raises(ValueError, match='20'), admitted(settings, 'b', 8):
            pass
        with admitted(settings, 'c', 7):
            assert len(list((tmp_path / 'ledger').glob('*.json'))) == 4
    assert sorted(p.name for p in (tmp_path / 'ledger').glob('*.json')) == ['gone.json', 'lean.json']


def test_a_run_the_ledger_refuses_leaves_no_directory_and_its_retry_is_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    box = FakeBox(METER, fail=('env -u EVALS_EVAL_LOG',))
    monkeypatch.setattr('evals.drive.Box.create', lambda *args, **kwargs: box)
    monkeypatch.setattr('evals.drive.image', lambda *args: None)
    settings = Settings(
        gateway='https://gw.example',
        gateway_token=SecretStr('t'),
        results=tmp_path / 'results',
        session_ledger=tmp_path / 'ledger',
        max_sessions=20,
    )
    (tmp_path / 'ledger').mkdir()
    later = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    full = tmp_path / 'ledger' / 'full.json'
    full.write_text(json.dumps({'run': 'other', 'sessions': 20, 'until': later}))
    arm = plain_run(settings).arm
    with pytest.raises(ValueError, match='needs 1 sessions'):
        drive(Task.load('t5-lpaq'), arm, 1, 10, 1, settings)
    out = tmp_path / 'results' / 't5-lpaq' / 'p-1'
    assert not out.exists(), 'a refused run leaves nothing behind'
    full.unlink()
    with pytest.raises(RuntimeError, match='exited 137'):
        drive(Task.load('t5-lpaq'), arm, 1, 10, 1, settings)
    assert (out / 'run.json').exists(), 'the retry was admitted and ran'


def test_each_task_names_its_scorer_and_daytona_refuses_a_task_that_needs_a_gpu_or_more_than_8_gib():
    scorers = {p.name: Task.load(p.name).scorer for p in sorted(TASKS.iterdir())}
    assert scorers == {
        't1-enwik8': 'modal',
        't2-circles': 'modal',
        't3-airbench': 'modal',
        't4-1brc': 'daytona',
        't5-lpaq': 'daytona',
        't6-optcomp': 'modal',
        't7-semantics': 'modal',
        't8-lean-foundations': 'modal',
    }
    t5 = Task.load('t5-lpaq').model_dump()
    for need in ({'gpu': 'L4'}, {'memory_limit_mb': 12288}):
        with pytest.raises(ValueError, match='daytona'):
            Task.model_validate(t5 | need)
    assert Task.model_validate(t5 | {'memory_limit_mb': 8192}).scorer == 'daytona'


def test_a_solved_task_leads_with_the_time_dollars_and_tokens_to_solve():
    from evals.report import solve_rows

    def point(hours: float, usd: float, sealed: float | None, tokens: int) -> Point:
        return Point(
            revision=f'r{hours}',
            hours=hours,
            usd=usd,
            models_usd=usd - 1,
            tokens=Tokens(input=tokens),
            dev=sealed,
            sealed=sealed,
        )

    solved = Result.model_construct(curve=[point(0, 0, 0.0, 0), point(0.4, 3, 0.5, 10), point(1.5, 9, 1.0, 40)])
    unsolved = Result.model_construct(curve=[point(0, 0, 0.0, 0), point(0.4, 3, 0.9, 10)])
    rows = {name: (value(solved), value(unsolved)) for name, value, _, _ in solve_rows(1.0)}
    assert rows['solved'] == (1.0, 0.0)
    assert rows['hours to solve'] == (1.5, None)
    assert rows['$ to solve'] == (9, None)
    assert rows['model $ to solve'] == (8, None)
    assert rows['tokens to solve'] == (40, None)
    assert rows['sealed score at 0.5 h'] == (0.5, 0.9)
    assert rows['sealed score at 1 h'] == (0.5, 0.9)
    assert rows['sealed score at 2 h'] == (1.0, 0.9)


def test_only_t6_is_scored_on_solving():
    assert Task.load('t6-optcomp').solve == 1.0
    assert [Task.load(name).solve for name in ('t3-airbench', 't4-1brc', 't5-lpaq')] == [None, None, None]


class Hung:
    """A platform's object whose every call hangs until ``released``."""

    def __init__(self) -> None:
        self.released = threading.Event()

    def __getattr__(self, name: str) -> Hung:
        return self

    def __call__(self, *args: object, **kwargs: object) -> Hung:
        self.released.wait()
        return self


def hung_box(kind: str, hung: Hung) -> Box | daytona.DaytonaBox:
    match kind:
        case 'modal':
            return Box(cast(modal.Sandbox, hung))
        case _:
            return daytona.DaytonaBox(cast(Daytona, hung), cast(Sandbox, hung), daytona.CONTROL)


@pytest.mark.parametrize('kind', ['modal', 'daytona'])
@pytest.mark.parametrize('verb', ['run', 'start', 'read', 'write', 'ended'])
def test_a_sandbox_call_that_hangs_fails_rather_than_holding_the_driver(
    monkeypatch: pytest.MonkeyPatch, kind: str, verb: str
) -> None:
    monkeypatch.setattr('evals.sandbox.CALL_SECONDS', 0.05)
    monkeypatch.setattr('evals.sandbox.MARGIN_SECONDS', 0)
    hung = Hung()
    box = hung_box(kind, hung)
    calls = {
        'run': lambda: box.run('true'),
        'start': lambda: box.start('true'),
        'read': lambda: box.read('/f'),
        'write': lambda: box.write('/f', b''),
        'ended': box.ended,
    }
    with pytest.raises(TimeoutError, match='did not answer'):
        calls[verb]()
    hung.released.set()


def test_a_control_probe_that_stalls_while_a_sample_is_tried_again_is_not_fatal(tmp_path: Path):
    class Stalling(FakeBox):
        def ended(self) -> bool:
            raise TimeoutError('Box.ended did not answer in 60 s')

    settings = Settings(poll_seconds=0)
    box = Stalling(METER, fail=('meter.py',), fail_once=True)
    assert (
        watch(fleet(box), plain_run(settings), 100, 0, settings, tmp_path / 'spend.jsonl', time.monotonic()) == 'hours'
    ), 'the next attempt samples'


def test_a_run_end_probe_that_stalls_is_asked_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(poll_seconds=0)
    run = plain_run(settings)
    answers = iter([TimeoutError('Box.run did not answer in 960 s'), 'the sessions ended: boom'])

    def stalling(box: object) -> str | None:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(run, 'ended', stalling)
    stopped = watch(fleet(FakeBox(METER)), run, 100, 6, settings, tmp_path / 'spend.jsonl', time.monotonic())
    assert stopped == 'the sessions ended: boom'


def test_a_spend_sample_that_times_out_is_tried_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(poll_seconds=0)
    box = FakeBox(METER)
    errors = iter([TimeoutError('Box.run did not answer in 960 s')])
    real = box.run

    def flaky(script: str, timeout: float | None = None) -> str:
        if 'meter.py' in script and (error := next(errors, None)) is not None:
            raise error
        return real(script, timeout)

    monkeypatch.setattr(box, 'run', flaky)
    assert (
        watch(fleet(box), plain_run(settings), 100, 0, settings, tmp_path / 'spend.jsonl', time.monotonic()) == 'hours'
    )


def test_a_run_whose_export_fails_writes_its_record_and_keeps_its_sandbox_for_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    box = FakeBox(METER, fail=('env -u EVALS_EVAL_LOG', 'bundle create'))
    monkeypatch.setattr('evals.drive.Box.create', lambda *args, **kwargs: box)
    monkeypatch.setattr('evals.drive.image', lambda *args: None)
    settings = Settings(
        gateway='https://gw.example',
        gateway_token=SecretStr('t'),
        results=tmp_path,
        session_ledger=tmp_path / 'ledger',
        poll_seconds=0,
    )
    with pytest.raises(RuntimeError, match='exited 137'):
        drive(Task.load('t5-lpaq'), plain_run(settings).arm, 1, 10, 0, settings)
    meta = json.loads((tmp_path / 't5-lpaq' / 'p-1' / 'run.json').read_text())
    assert meta['export_failed'][0].startswith('control: RuntimeError') and meta['kept'] == ['sb-fake']
    assert not box.closed, 'a sandbox that could not give its export waits for inspection, until its TTL'


def test_a_pool_run_s_heads_are_its_best_over_time_and_its_line_holds_what_stacked(db: Database) -> None:
    """A piloop with a pool (0.6) has no verdicts: its curve is the best that each score names, and its team counts
    the candidates on the final best's line. A database at piloop's head is one; a verdict run's is not."""
    with closing(sqlite3.connect(db.engine.url.database or '')) as at_head:
        assert pool(at_head)
    with closing(sqlite3.connect(':memory:')) as con:
        con.executescript(
            """
            create table campaigns (id integer, evaluator_id integer);
            create table evaluators (id integer, definition text);
            create table runs (id integer, role text);
            create table candidates (id integer, revision text, parent_id integer, run_id integer, origin text);
            create table measurements (candidate_id integer, evaluator_id integer, error text, rung text);
            create table events (id integer, target_kind text, target_id integer, kind text, at text, payload text);
            insert into campaigns values (1, 1);
            insert into evaluators values (1, '{"evaluate": []}'), (2, '{"sealed": ["s"]}');
            insert into runs values (1, 'implementer'), (2, 'implementer');
            insert into candidates values (1, 'base', null, null, 'baseline'), (2, 'a', 1, 1, 'run'),
                (3, 'b', 2, 2, 'run'), (4, 'c', 1, 2, 'run');
            insert into measurements values (1, 1, null, null), (2, 1, null, null), (2, 1, null, null),
                (3, 1, null, null), (3, 1, null, null), (4, 1, null, null), (2, 2, null, null), (1, 2, null, null),
                (3, 2, null, null);
            """
        )
        scored = [(2, 2, '00:30'), (4, 2, '01:00'), (3, 3, '02:00'), (3, 3, '02:30')]
        for i, (target, best, at_) in enumerate(scored, start=1):
            payload = json.dumps({'kind': 'candidate.scored', 'best': best})
            con.execute(
                "insert into events values (?, 'candidate', ?, 'candidate.scored', ?, ?)",
                (i, target, f'2026-01-01 {at_}:00', payload),
            )
        chosen = json.dumps({'kind': 'run.base_chosen', 'candidate': 2, 'was': 1})
        con.execute("insert into events values (9, 'run', 2, 'run.base_chosen', '2026-01-01 01:30:00', ?)", (chosen,))
        started = datetime(2026, 1, 1, tzinfo=UTC)
        heads = pool_heads(con, 'base', started)
        assert heads == [('a', 0.5), ('b', 2.0)], 'a score of a candidate that is not the best moves nothing'
        assert final_line(con, heads) == ({'implementer': 2}, {'implementer': 2}), 'b, and a under it; not c'
        assert pool_counts(con) == PoolCounts(confirmations=2, sealed_scorings=3, based_runs=1)


def test_no_test_reads_this_machines_session_ledger(tmp_path: Path):
    ledger = Settings().session_ledger
    assert ledger != Path('/tmp/evals-sessions')
    assert ledger.is_relative_to(tmp_path)


def test_the_lean_tasks_share_one_evaluator_scorer_and_audit():
    for path in ('repo/evaluate.py', 'sealed.py', 'repo/check/Audit.lean', 'repo/LICENSE'):
        for task in ('t7-semantics', 't8-lean-foundations'):
            assert (TASKS / 't6-optcomp' / path).read_bytes() == (TASKS / task / path).read_bytes(), (task, path)


def test_a_plain_agents_box_asks_for_8_gib_with_no_hard_limit_unless_its_task_has_one(monkeypatch: pytest.MonkeyPatch):
    made: list[dict[str, object]] = []
    monkeypatch.setattr('evals.drive.Box.create', lambda *args, **kwargs: made.append(kwargs) or FakeBox())
    monkeypatch.setattr('evals.drive.image', lambda *args: None)
    arm = plain_run(Settings()).arm
    for task in ('t5-lpaq', 't6-optcomp'):
        agent_box(Task.load(task), arm, Settings(), 6, {}, 'r')
    assert [(m['memory_mb'], m['memory_limit_mb']) for m in made] == [(8192, None), (12288, 12288)]


def commit(repo: Path, name: str, text: str | None = None) -> str:
    """Commit ``name`` with ``text`` (its name by default) into ``repo``; its revision."""
    (repo / name).write_text(name if text is None else text)
    git(repo, 'add', name)
    git(repo, 'commit', '-qm', name)
    return git(repo, 'rev-parse', 'HEAD').strip()


def candidates(db: Database, campaign: Campaign, made: list[tuple[str, Origin, int | None]]) -> list[int]:
    """Add ``(revision, origin, index of the parent in made)`` candidates after the baseline; their ids."""
    with db.session() as s:
        ids = [s.scalars(select(Candidate.id).where(Candidate.campaign_id == campaign.id)).one()]
        for i, (revision, origin, parent) in enumerate(made, start=1):
            c = Candidate(
                campaign_id=campaign.id,
                tree_sha=f'tree{i}',
                revision=revision,
                origin=origin,
                parent_id=None if parent is None else ids[parent],
            )
            s.add(c)
            s.flush()
            ids.append(c.id)
        s.commit()
    return ids


def test_a_piloop_run_s_submissions_are_its_implementers_candidates_each_with_the_one_it_came_from(
    db: Database, campaign: Campaign
) -> None:
    """A candidate that was recovered, skipped or imported was never submitted, but a submission may come from one."""
    base = baseline(db, campaign.id).revision
    candidates(
        db,
        campaign,
        [
            ('a', Origin.RUN, 0),
            ('recovered', Origin.RECOVERED, 1),
            ('skipped', Origin.SKIPPED, 1),
            ('reference', Origin.REFERENCE, None),
            ('b', Origin.RUN, 2),
            ('c', Origin.RUN, 1),
        ],
    )
    with closing(sqlite3.connect(db.engine.url.database or '')) as con:
        assert pool(con)
        assert piloop_submissions(con) == [('a', base), ('b', 'recovered'), ('c', 'a')]


def test_a_plain_run_s_submissions_are_main_s_first_parent_commits_each_with_its_first_parent(tmp_path: Path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    base = commit(repo, 'base')
    one = commit(repo, 'one')
    git(repo, 'checkout', '-q', '-b', 'side')
    side = commit(repo, 'side')
    git(repo, 'checkout', '-q', 'main')
    two = commit(repo, 'two')
    git(repo, 'merge', '-q', '--no-ff', '-m', 'merge', 'side')
    merged = git(repo, 'rev-parse', 'HEAD').strip()
    git(repo, 'checkout', '-q', '-b', 'elsewhere')
    commit(repo, 'never on main')
    bundle = tmp_path / 'repo.bundle'
    git(repo, 'bundle', 'create', str(bundle), '--all')
    assert plain_submissions(bundle, base) == [(one, base), (two, one), (merged, two)], f'not {side}, merged in'


def scoring_of(passed: dict[str, bool] | None, sealed: float | None = 0.5) -> Scoring:
    return Scoring(dev=None, sealed=sealed, detail='', cpu='c', items=passed)


def test_each_tree_is_scored_once_and_its_score_goes_to_every_revision_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q', '-b', 'main')
    base = commit(repo, 'f', 'base')
    changed = commit(repo, 'f', 'changed')
    reverted = commit(repo, 'f', 'base')
    git(repo, 'commit', '-q', '--allow-empty', '-m', 'empty')
    empty = git(repo, 'rev-parse', 'HEAD').strip()
    bundle = tmp_path / 'repo.bundle'
    git(repo, 'bundle', 'create', str(bundle), '--all')
    scored: list[str] = []
    lock = threading.Lock()

    def content(tgz: bytes) -> str:
        with tarfile.open(fileobj=BytesIO(tgz)) as tar:
            f = tar.extractfile('f')
            assert f is not None
            return f.read().decode()

    def fake_one(task: Task, rev: str, base_tar: bytes, tree: bytes, *_: object) -> Scoring:
        assert content(base_tar) == 'base'
        with lock:
            scored.append(content(tree))
        return scoring_of({content(tree): True})

    monkeypatch.setattr(scoring, 'score_one', fake_one)
    monkeypatch.setattr(scoring, 'sealed_data', lambda name, settings: b'')
    task = Task.load('t2-circles').model_copy(update={'score_repeats': 2})
    found = score(task, bundle, base, [changed, reverted, empty, changed], Settings())
    assert sorted(scored) == ['base', 'base', 'changed', 'changed'], 'two trees, two repeats each; not the base'
    assert set(found) == {changed, reverted, empty}
    assert found[reverted] is found[empty] and found[reverted].items == {'base': True}
    assert found[changed].items == {'changed': True}


def test_a_score_passes_an_item_only_when_every_scoring_did_and_a_crash_passes_none():
    assert Score.of([scoring_of({'a': True, 'b': True}), scoring_of({'a': True, 'b': False})]).items == {
        'a': True,
        'b': False,
    }
    crashed = Score.of([scoring_of({'a': True}), scoring_of(None, sealed=None)])
    assert crashed.items == {'a': False} and crashed.sealed is None
    assert Score.of([scoring_of(None)]).items is None


def test_a_submission_regresses_when_it_fails_an_item_that_its_parent_passed():
    def of(passed: dict[str, bool] | None, sealed: float | None = 0.5) -> Score:
        return Score.of([scoring_of(passed, sealed)])

    scores = {
        'base': of({'a': True, 'b': False, 'c': True}),
        'gains-b-loses-c': of({'a': True, 'b': True, 'c': False}),
        'crash': of(None, sealed=None),
        'after-crash': of({'a': True, 'b': False, 'c': False}),
        'same': of({'a': True, 'b': False, 'c': True}),
    }
    made = [('gains-b-loses-c', 'base'), ('crash', 'gains-b-loses-c'), ('after-crash', 'crash'), ('same', 'base')]
    found = regressions([*made, ('unscored', 'base')], scores, 'base')
    assert found.found == [
        Regression(revision='gains-b-loses-c', parent='base', lost=['c']),
        Regression(revision='crash', parent='gains-b-loses-c', lost=['a', 'b']),
    ], 'a gained item does not make up for a lost one; a crash fails every item; nothing is lost from a crash'
    assert (found.submissions, found.scored, found.share) == (5, 4, 0.5)
    bare = {rev: Score(dev=None, sealed=0.5, detail='') for rev in scores}
    assert regressions(made, bare, 'base') == Regressions(submissions=4, scored=4, found=None)
    assert Regressions(submissions=4, scored=4, found=None).share is None


def pool_run(tmp_path: Path, db: Database, campaign: Campaign) -> tuple[Path, str, str, str]:
    """A pool run's directory as an older driver left it: its base and head scored without items, and no result.

    Its candidates: ``a`` from the base, the best; ``b`` from ``a``, never the best. Its base, ``a`` and ``b``.
    """
    repo = Path(campaign.path)
    base = baseline(db, campaign.id).revision
    a = commit(repo, 'solve.txt', 'a\n')
    b = commit(repo, 'solve.txt', 'b\n')
    run = tmp_path / 'run'
    out = run / 'export' / 'out'
    out.mkdir(parents=True)
    git(repo, 'bundle', 'create', str(out / 'repo.bundle'), '--all')
    ids = candidates(db, campaign, [(a, Origin.RUN, 0), (b, Origin.RUN, 1)])
    with db.session() as s:
        payload = json.dumps({'kind': 'candidate.scored', 'best': ids[1]})
        s.connection().exec_driver_sql(
            "insert into events (at, actor, target_kind, target_id, payload) values (?, 'test', 'candidate', ?, ?)",
            ('2026-01-01 00:30:00', ids[1], payload),
        )
        s.commit()
    with (
        closing(sqlite3.connect(db.engine.url.database or '')) as live,
        closing(sqlite3.connect(out / 'piloop.db')) as kept,
    ):
        live.backup(kept)  # a copy of the file would miss what is still in the write-ahead log
    meta = {'task': 't2-circles', 'arm': 'pool-1w', 'repeat': 1, 'piloop': 'piloop 0.6', 'base': base}
    meta |= {'started_at': '2026-01-01T00:00:00+00:00', 'budget_usd': 20, 'max_hours': 4, 'stopped': 'hours'}
    (run / 'run.json').write_text(json.dumps(meta))
    (run / 'spend.jsonl').write_text(Sample(hours=1, models_usd=1, compute_usd=0).model_dump_json())
    old = {'dev': 0.1, 'sealed': 0.1, 'detail': ''}
    (run / 'scores.json').write_text(json.dumps({base: old, a: old}))
    return run, base, a, b


def test_rescoring_an_old_run_scores_its_submissions_only_when_the_task_reports_items(
    tmp_path: Path, db: Database, campaign: Campaign, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, base, a, b = pool_run(tmp_path, db, campaign)
    asked: list[list[str]] = []
    items = {base: {'x': True, 'y': False}, a: {'x': True, 'y': True}, b: {'x': False, 'y': True}}

    def fake_score(
        task: Task, bundle: Path, base_rev: str, revisions: list[str], settings: Settings
    ) -> dict[str, Score]:
        asked.append(revisions)
        return {rev: Score.of([scoring_of(items[rev])]) for rev in revisions}

    monkeypatch.setattr(pilot, 'score', fake_score)
    pilot.scored(run, Settings())
    assert asked == [], 'the base and the head were scored, and the base has no items'
    r = Result.model_validate_json((run / 'result.json').read_text())
    assert r.regressions == Regressions(submissions=2, scored=1, found=None)
    assert [p.revision for p in r.curve] == [base, a]
    (run / 'scores.json').unlink()
    pilot.scored(run, Settings())
    assert asked == [[base, a], [b]], 'the base and the head first; then the submission that is not a head'
    r = Result.model_validate_json((run / 'result.json').read_text())
    assert r.regressions == Regressions(submissions=2, scored=2, found=[Regression(revision=b, parent=a, lost=['x'])])
    assert r.regressions.share == 0.5
