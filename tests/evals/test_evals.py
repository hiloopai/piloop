import importlib.util
import json
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import JsonValue, SecretStr

from evals import meter
from evals.drive import HALT, Plain, piloop_config, price
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
    load_arm,
)
from evals.pilot import guard, spent
from evals.report import plain_bar, write
from evals.score import Score, Scoring
from evals.team import final_keeps, notes, races
from evals.telemetry import exported


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
    (tmp_path / 'holdout.bin').write_text('121')
    assert sealed.main(tmp_path, tmp_path, tmp_path)['status'] == 'over_limit'
    (tmp_path / 'holdout.bin').write_text('119')
    assert sealed.main(tmp_path, tmp_path, tmp_path)['score'] == pytest.approx(0.5)


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
