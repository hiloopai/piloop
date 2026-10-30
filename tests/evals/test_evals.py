import importlib.util
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import JsonValue, SecretStr

from evals import meter
from evals.drive import piloop_config, price
from evals.metrics import at, result
from evals.model import (
    ARMS,
    TASKS,
    Funnel,
    PiloopArm,
    PlainArm,
    Point,
    Resources,
    Result,
    Sample,
    Settings,
    Task,
    load_arm,
)
from evals.report import write
from evals.score import Score
from evals.telemetry import exported


def module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tasks_and_arms_load():
    tasks = [Task.load(p.name) for p in sorted(TASKS.iterdir())]
    assert [t.name for t in tasks] == ['t1-enwik8', 't2-circles']
    assert all(t.normalised(t.dev_base) == 0 and t.normalised(t.dev_ref) == 1 for t in tasks)
    arms = {p.stem: load_arm(p.stem) for p in ARMS.glob('*.toml')}
    assert {type(a) for a in arms.values()} == {PiloopArm, PlainArm}


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
    control = load_arm('piloop-0.5.0')
    assert isinstance(control, PiloopArm)
    arm = PiloopArm(
        kind='piloop',
        name='a',
        piloop='piloop==0.5.0',
        agents=control.agents,
        overlay={'plugins': {'role': {'implementer': {'max_concurrent': 2}}}},
        resources=Resources(cpu=1, memory_mb=512),
    )
    config = piloop_config(Task.load('t2-circles'), arm, settings, 4)
    assert get(config, 'plugins', 'stop_rule') == {'max_hours': 4}
    assert get(config, 'plugins', 'role') == {'implementer': {'max_concurrent': 2}}
    assert get(config, 'plugins', 'harness', 'use') == ['codex', 'claude']
    assert get(config, 'plugins', 'harness', 'claude', 'env', 'ANTHROPIC_BASE_URL') == 'https://gw.example'
    assert config['name'] == 't2-circles'


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
    spend = [Sample(hours=h, models_usd=10 * h, compute_usd=0) for h in (0, 1, 2, 3)]
    (run / 'spend.jsonl').write_text('\n'.join(s.model_dump_json() for s in spend))
    scores = {rev: Score(dev=i / 3, sealed=i / 4, detail='') for i, rev in enumerate(revs)}
    r = result(run, scores)
    assert [(p.revision, p.usd) for p in r.curve] == [(revs[0], 0), (revs[1], 5), (revs[2], 15)]
    assert r.funnel == Funnel(ideas=None, runs=2, submissions=3, kept=3)
    assert r.first_keep is not None and r.first_keep.usd == 5
    assert r.gap == pytest.approx(2 / 3 - 2 / 4)
    other = r.model_copy(
        update={'arm': 'plain-claude', 'curve': [Point(revision='b', hours=0, usd=0, dev=0, sealed=None)]}
    )
    [page] = write([r, other], tmp_path / 'site')
    text = page.read_text()
    assert page.name == 't2-circles.html' and 'plain-claude' in text and 'plotly' in text
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
