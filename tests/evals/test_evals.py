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
import warnings
from collections import Counter
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import JsonValue, SecretStr

from evals import meter
from evals.drive import HALT, Piloop, Plain, drive, piloop_config, price, sample, sandbox, watch
from evals.loop import evaluations
from evals.metrics import at, result
from evals.model import (
    ARMS,
    ROOT,
    TASKS,
    Agent,
    Funnel,
    PiloopArm,
    PlainArm,
    Point,
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
from evals.score import Score, Scoring
from evals.team import final_keeps, notes, races
from evals.telemetry import exported
from piloop.config import CampaignConfig
from piloop.deprecated import PiloopDeprecationWarning


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
            if not arm.charters or task.name in arm.charters:  # an arm with charters runs only on their tasks
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
        resources=Resources(cpu=1, memory_mb=512),
    )
    config = piloop_config(Task.load('t2-circles'), arm, settings, 4)
    assert get(config, 'plugins', 'stop_rule') == {'max_hours': 4}
    assert get(config, 'plugins', 'role') == {'implementer': {'max_concurrent': 2}}
    assert get(config, 'plugins', 'harness', 'use') == ['codex', 'claude']
    assert get(config, 'plugins', 'harness', 'claude', 'env', 'ANTHROPIC_BASE_URL') == 'https://gw.example'
    assert config['name'] == 't2-circles'


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
        self.closed = False
        self.sandbox = SimpleNamespace(object_id='sb-fake')

    def run(self, script: str, timeout: float | None = None) -> str:
        self.commands.append(script)
        for marker in self.fail:
            if marker in script and not (self.fail_once and marker in self.failed):
                self.failed.add(marker)
                raise RuntimeError(f'{script[:40]!r} exited 137: ')
        return self.out

    def start(self, script: str) -> None:
        self.commands.append(script)

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
        pass

    def ended(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


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
    stopped = watch(
        box, run, Resources(cpu=1, memory_mb=512), 100, 0, settings, tmp_path / 'spend.jsonl', time.monotonic()
    )
    assert stopped == 'hours'
    assert len((tmp_path / 'spend.jsonl').read_text().splitlines()) == 2


def test_a_spend_sample_that_keeps_failing_stops_the_run_and_says_why(tmp_path: Path):
    settings = Settings(poll_seconds=0, sample_attempts=2)
    box = FakeBox(METER, fail=('meter.py',))
    run = plain_run(settings)
    stopped = watch(
        box, run, Resources(cpu=1, memory_mb=512), 100, 6, settings, tmp_path / 'spend.jsonl', time.monotonic()
    )
    assert stopped.startswith('sample failed:') and 'exited 137' in stopped
    assert any('pkill' in c for c in box.commands)


def test_a_fatal_error_still_exports_the_run_before_its_sandbox_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    box = FakeBox(METER, fail=('env -u EVALS_EVAL_LOG',))
    monkeypatch.setattr('evals.drive.Box.create', lambda *args, **kwargs: box)
    monkeypatch.setattr('evals.drive.image', lambda *args: None)
    settings = Settings(gateway='https://gw.example', gateway_token=SecretStr('t'), results=tmp_path)
    arm = plain_run(settings).arm
    with pytest.raises(RuntimeError, match='exited 137'):
        drive(Task.load('t5-lpaq'), arm, 1, 10, 1, settings)
    out = tmp_path / 't5-lpaq' / 'p-1'
    assert any('bundle create' in c for c in box.commands) and box.closed
    assert (out / 'export' / 'out' / 'sessions').exists()
    assert json.loads((out / 'run.json').read_text())['stopped'].startswith('error: RuntimeError')


def test_a_sample_keeps_the_meters_tokens_and_the_sandboxs_compute():
    count = {'input': 10, 'output': 5, 'cache_read': 100, 'cache_write': 1, 'turns': 1}
    box = FakeBox({'claude': {'claude-opus-5-5': count}, 'codex': {'gpt-6.1-sol': count}})
    settings = Settings(cpu_usd_per_core_hour=1, memory_usd_per_gib_hour=0.5)
    s = sample(box, Resources(cpu=40, memory_mb=4096), settings, 2)
    assert s.tokens == Tokens(input=20, output=10, cache_read=200, cache_write=2, turns=2)
    assert s.compute_usd == 2 * (40 + 4 * 0.5) and s.models_usd > 0


def identical(n: int) -> PiloopArm:
    return PiloopArm(
        kind='piloop',
        name=f'{n}w',
        piloop='piloop==0.6.0',
        agents=[Agent(harness='codex', model='gpt-6.1-sol')],
        overlay={'plugins': {'role': {'implementer': {'max_concurrent': n}}}},
        resources=Resources(cpu=8, memory_mb=16384),
    )


def test_a_piloop_arms_sandbox_and_evaluation_slots_grow_with_its_workers():
    task, settings = Task.load('t5-lpaq'), Settings()
    assert sandbox(task, identical(1), settings, 6) == (Resources(cpu=8, memory_mb=16384), 2)
    assert sandbox(task, identical(20), settings, 6) == (Resources(cpu=40, memory_mb=20 * 6144), 40)
    roles = {'implementer': {'max_concurrent': 1}, 'implementer_b': {'runs': True, 'max_concurrent': 2}}
    charters = identical(1).model_copy(update={'overlay': {'plugins': {'role': roles}}})
    assert sandbox(task, charters, settings, 6) == (Resources(cpu=8, memory_mb=3 * 6144), 6)


def test_a_piloop_campaign_gets_its_evaluation_slots_before_it_starts():
    run = Piloop(Task.load('t2-circles'), identical(20), Settings(), 6, slots=40)
    box = FakeBox()
    run.start(box)
    limits = next(i for i, c in enumerate(box.commands) if 'piloop limits set' in c)
    init = next(i for i, c in enumerate(box.commands) if 'piloop init' in c)
    assert 'executor.pools.serve.slots.evaluation=40' in box.commands[limits] and limits < init


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
    assert page.name == 't2-circles.html' and 'plain-claude' in text and 'plotly' in text
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


def test_charters_are_each_roles_addendum_on_their_task():
    arm = load_arm('c-charters-2')
    assert isinstance(arm, PiloopArm)
    config = piloop_config(Task.load('t4-1brc'), arm, Settings(), 4)
    roles = get(config, 'plugins', 'role')
    assert isinstance(roles, dict)
    assert {r: v.get('addendum') for r, v in roles.items() if isinstance(v, dict) and 'addendum' in v} == {
        'implementer': 'charter-implementer.md',
        'implementer_b': 'charter-implementer_b.md',
    }
    assert all(len(text.split()) <= 60 for charter in arm.charters.values() for text in charter.values())
    with pytest.raises(ValueError, match='not for t2-circles'):
        piloop_config(Task.load('t2-circles'), arm, Settings(), 4)


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
    db = sqlite3.connect(':memory:')
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
    assert final_keeps(db, bundle) == ({'implementer': 2, 'implementer_b': 1}, {'implementer': 1, 'implementer_b': 0})
    for i, (verdict, parent, best) in enumerate((('discard', 5, -1), ('discard', -2, -3), ('keep', 4, 4)), start=10):
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
    assert notes(db, tmp_path / 'export') == ({'implementer': 1, 'implementer_b': 1}, {'implementer>implementer_b': 1})
    db.close()


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
