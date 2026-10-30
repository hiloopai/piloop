"""Run one arm on one task in its own sandbox, within a $ and a time cap, and export what it left.

The driver reads the arm's spend every ``poll_seconds``: the tokens of every agent session in the sandbox, at list
price, plus the sandbox's own CPU and memory. It stops the run at the first of the budget, ``max_hours``, or a
campaign's own stop. The export is ``export/``: the repo as a git bundle, piloop's database for a piloop arm, the
agent session files, and ``spend.jsonl``, beside ``run.json``.
"""

import json
import shlex
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import boto3
import modal
import tomli_w
from opentelemetry import trace
from pydantic import JsonValue

from evals.model import PiloopArm, PlainArm, Sample, Settings, Task
from evals.sandbox import OPT, PILOOP_HOME, WORK, Box, image
from evals.telemetry import exported, tracer
from piloop.plugins.harness.codex import toml
from piloop.plugins.harness.contract import ModelId, Usage
from piloop.plugins.price_list.genai_prices import GenaiPrices

REPO = f'{WORK}/repo'
OUT = f'{WORK}/out'
SESSIONS = {'claude': '/root/.claude/projects', 'codex': '/root/.codex/sessions'}
VENDORS = {'claude': 'anthropic', 'codex': 'openai'}
BASE_DATE = '2026-01-01T00:00:00Z'
"""The date of the starting commit, so that every arm starts from the same commit id."""
PLAIN_PROMPT = """You work alone in this repository, and no one will answer questions. Read brief.md and do the task.

You own both the change and the measurement: run `{dev}` as often as you like.
Keep `main` at your best version. Commit each version that beats your best so far, with a message that says what
changed and its score, and do not commit one that does not. Your last commit on `main` is what you hand in.
You have about {hours:g} hours. Work until you are stopped; when you run out of ideas, try bolder ones."""
CONTINUE_PROMPT = 'You still have time. Keep improving: commit each version that beats your best so far.'


def merge(base: dict[str, JsonValue], over: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """``over`` merged into ``base``, table by table."""
    merged = dict(base)
    for key, value in over.items():
        old = merged.get(key)
        merged[key] = merge(old, value) if isinstance(old, dict) and isinstance(value, dict) else value
    return merged


def codex_gateway(settings: Settings) -> dict[str, JsonValue]:
    """The ``-c`` keys that send Codex to the gateway, with the token from ``GATEWAY_TOKEN``."""
    provider = {'name': 'gateway', 'base_url': f'{settings.gateway}/v1', 'env_key': 'GATEWAY_TOKEN'}
    return {'model_provider': 'gateway', 'model_providers': {'gateway': {**provider, 'wire_api': 'responses'}}}


def piloop_config(task: Task, arm: PiloopArm, settings: Settings, hours: float) -> dict[str, JsonValue]:
    """The campaign's ``piloop.toml``: the task's keys, the arm's agents on the gateway, the time cap, the overlay."""
    harness: dict[str, JsonValue] = {'use': [a.harness for a in arm.agents]}
    for agent in arm.agents:
        match agent.harness:
            case 'codex':
                harness['codex'] = {
                    'model': agent.model,
                    'user_config': 'ignore',
                    'config': codex_gateway(settings),
                    'env': {'GATEWAY_TOKEN': {'secret': 'GATEWAY_TOKEN'}},
                }
            case 'claude':
                harness['claude'] = {
                    'model': agent.model,
                    'user_config': 'ignore',
                    'env': {
                        'ANTHROPIC_BASE_URL': settings.gateway,
                        'ANTHROPIC_AUTH_TOKEN': {'secret': 'GATEWAY_TOKEN'},
                    },
                }
    own: dict[str, JsonValue] = {
        'plugins': {'runtime': {'use': 'subprocess'}, 'harness': harness, 'stop_rule': {'max_hours': hours}}
    }
    return merge(merge(task.piloop, own), arm.overlay)


def price(tokens: dict[str, dict[str, dict[str, int]]]) -> float:
    """The list price of the meter's tokens. A model without a price raises: an unknown cost is never 0."""
    prices = GenaiPrices()
    usd = 0.0
    for harness, models in tokens.items():
        for model, t in models.items():
            usage = Usage(
                input=t['input'],
                output=t['output'],
                cache_read=t['cache_read'],
                cache_write=t['cache_write'],
                total=t['input'] + t['output'] + t['cache_read'] + t['cache_write'],
                turns=t['turns'],
            )
            cost = prices.usd(ModelId(VENDORS[harness], model), usage)
            if cost is None:
                raise ValueError(f'no list price for {harness} model {model!r}')
            usd += cost
    return usd


@dataclass
class Piloop:
    """A piloop campaign in the sandbox, under its own ``PILOOP_HOME``."""

    task: Task
    arm: PiloopArm
    settings: Settings
    hours: float
    version: str = ''

    @property
    def name(self) -> str:
        return str(self.task.piloop['name'])

    def sql(self, box: Box, query: str) -> str:
        return box.run(f'sqlite3 {PILOOP_HOME}/piloop.db {shlex.quote(query)}').strip()

    def start(self, box: Box) -> None:
        config = piloop_config(self.task, self.arm, self.settings, self.hours)
        box.write(f'{REPO}/piloop.toml', tomli_w.dumps(config).encode())
        box.run(
            f'cd {REPO} && echo piloop.toml >> .git/info/exclude && piloop serve --detach'
            ' && printf %s "$GATEWAY_TOKEN" | piloop secret set GATEWAY_TOKEN'
            f' && piloop init . > {OUT}/init.log 2>&1',
            timeout=1800,
        )
        self.version = box.run('piloop --version').strip()
        trace.get_current_span().set_attribute('piloop.version', self.version)

    def ended(self, box: Box) -> str | None:
        """The campaign's own stop reason, once it stopped."""
        status = self.sql(
            box, f"select status || '|' || coalesce(stopped_reason, '') from campaigns where name = '{self.name}'"
        )
        state, _, reason = status.partition('|')
        return None if state == 'active' else f'campaign: {reason}'

    def stop(self, box: Box, reason: str) -> None:
        """Stop at once and give the daemon ``stop_grace_minutes`` to score what the agents committed."""
        box.run(f'piloop campaign stop {self.name} --kill --reason {shlex.quote(reason)} || true')
        deadline = time.monotonic() + self.settings.stop_grace_minutes * 60
        pending = "select count(*) from runs where status in ('queued', 'running') or scoring"
        while time.monotonic() < deadline and self.sql(box, pending) != '0':
            time.sleep(self.settings.poll_seconds / 4)

    def export(self, box: Box) -> None:
        box.run(f'sqlite3 {PILOOP_HOME}/piloop.db ".backup {OUT}/piloop.db" && piloop stop || true')


@dataclass
class Plain:
    """One agent session, resumed with ``CONTINUE_PROMPT`` each time it ends before the run does."""

    task: Task
    arm: PlainArm
    settings: Settings
    hours: float
    session: str = ''
    sessions: int = 0
    proc: modal.container_process.ContainerProcess[str] | None = field(default=None, repr=False)

    def argv(self, prompt: str) -> list[str]:
        """The headless argv of the first session, or of a resume of it."""
        agent = self.arm.agent
        match agent.harness:
            case 'codex':
                flags = ['--json', '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check']
                flags += ['--model', agent.model, '-c', f'model_reasoning_effort="{self.arm.effort}"']
                for key, value in codex_gateway(self.settings).items():
                    flags += ['-c', f'{key}={toml(value)}']
                head = ['codex', 'exec', 'resume', *flags, self.session] if self.sessions else ['codex', 'exec', *flags]
                return [*head, prompt]
            case 'claude':
                session = ['--resume', self.session] if self.sessions else ['--session-id', self.session]
                flags = ['--model', agent.model, '--effort', self.arm.effort, '--dangerously-skip-permissions']
                return ['claude', '-p', '--output-format', 'stream-json', '--verbose', *session, *flags, prompt]

    def launch(self, box: Box, prompt: str) -> None:
        match self.arm.agent.harness:
            case 'claude':
                env = f'ANTHROPIC_BASE_URL={shlex.quote(self.settings.gateway)} ANTHROPIC_AUTH_TOKEN="$GATEWAY_TOKEN"'
            case 'codex':
                env = ''  # its gateway is in its flags
        log = f'{OUT}/session-{self.sessions + 1}.jsonl'
        self.proc = box.start(f'cd {REPO} && {env} {shlex.join(self.argv(prompt))} < /dev/null > {log} 2>&1')
        if self.arm.agent.harness == 'codex' and not self.sessions:
            self.session = self.thread(box, log)
        self.sessions += 1

    def thread(self, box: Box, log: str) -> str:
        """Codex's thread id, from the line of its output that names it."""
        deadline = time.monotonic() + self.settings.codex_thread_seconds
        while time.monotonic() < deadline:
            first = box.run(f'grep -m 1 thread_id {log} 2>/dev/null || true')
            if first:
                return json.loads(first)['thread_id']
            time.sleep(1)
        raise RuntimeError(f'codex printed no thread id: {box.run(f"tail -c 2000 {log}")}')

    def start(self, box: Box) -> None:
        self.session = str(uuid.uuid4())
        self.launch(box, PLAIN_PROMPT.format(dev=shlex.join(self.task.dev), hours=self.hours))

    def ended(self, box: Box) -> str | None:
        """Never on its own: a session that ended is resumed."""
        if self.proc is not None and self.proc.poll() is not None:
            with tracer.start_as_current_span('evals.resume', attributes={'session': self.sessions + 1}):
                self.launch(box, CONTINUE_PROMPT)
        return None

    def stop(self, box: Box, reason: str) -> None:
        box.run('pkill -f "[c]odex exec|[c]laude -p" || true')

    def export(self, box: Box) -> None:
        box.run(f'echo {self.sessions} > {OUT}/sessions')


def prepare(box: Box, task: Task) -> str:
    """Copy the task's repo and data into ``REPO``, make the starting commit, install its deps. Its commit id."""
    data = f'{OPT}/data/{task.name}'
    return box.run(
        f'mkdir -p {REPO} {OUT} && cp -r {OPT}/tasks/{task.name}/repo/. {REPO}/'
        f' && if [ -d {data} ]; then cp -r {data}/. {REPO}/; fi'
        f' && cd {REPO} && git init -q && git add -A'
        f' && GIT_AUTHOR_DATE={BASE_DATE} GIT_COMMITTER_DATE={BASE_DATE} git commit -qm base'
        ' && uv sync -q && git rev-parse HEAD',
        timeout=900,
    ).strip()


def sample(box: Box, arm: PiloopArm | PlainArm, settings: Settings, hours: float) -> Sample:
    """What the run has spent so far."""
    box.run(f'python3 {OPT}/meter.py {SESSIONS["claude"]} {SESSIONS["codex"]} {WORK}/meter.json')
    tokens = json.loads(box.read(f'{WORK}/meter.json'))
    r = arm.resources
    compute = hours * (r.cpu * settings.cpu_usd_per_core_hour + r.memory_mb / 1024 * settings.memory_usd_per_gib_hour)
    return Sample(hours=hours, models_usd=price(tokens), compute_usd=compute)


def drive(task: Task, arm: PiloopArm | PlainArm, repeat: int, budget: float, hours: float, settings: Settings) -> Path:
    """Run ``arm`` on ``task`` and export it to ``results/<task>/<arm>-<repeat>/``. Its directory."""
    if not settings.gateway or not settings.gateway_token.get_secret_value():
        raise ValueError('set EVALS_GATEWAY and EVALS_GATEWAY_TOKEN: the agents need a model gateway')
    out = settings.results / task.name / f'{arm.name}-{repeat}'
    out.mkdir(parents=True, exist_ok=False)
    name = f'{task.name}/{arm.name}-{repeat}'
    match arm:
        case PiloopArm():
            run: Piloop | Plain = Piloop(task, arm, settings, hours)
            env = exported(settings, name)
        case PlainArm():
            run = Plain(task, arm, settings, hours)
            env = {}
    r = arm.resources
    with tracer.start_as_current_span('evals.run', attributes={'evals.run': name, 'budget_usd': budget}) as span:
        with tracer.start_as_current_span('evals.sandbox'):
            img = image([task], arm)
            box = Box.create(
                settings, img, cpu=r.cpu, memory_mb=r.memory_mb, hours=hours + 1, network=True, env=env, run=name
            )
        span.set_attribute('modal.sandbox', box.sandbox.object_id)
        try:
            with tracer.start_as_current_span('evals.prepare'):
                base = prepare(box, task)
            with tracer.start_as_current_span('evals.start'):
                run.start(box)
            started_at, started = datetime.now(UTC), time.monotonic()
            stopped = watch(box, run, arm, budget, hours, settings, out / 'spend.jsonl', started)
            span.set_attribute('stopped', stopped)
            with tracer.start_as_current_span('evals.export'):
                run.export(box)
                box.run(
                    f'git -C {REPO} bundle create {OUT}/repo.bundle --all'
                    f' && tar czf {WORK}/export.tgz --ignore-failed-read -C {WORK} out'
                    ' -C /root .claude/projects .codex/sessions',
                    timeout=900,
                )
                (out / 'export.tgz').write_bytes(box.read(f'{WORK}/export.tgz'))
        finally:
            box.close()
    with tarfile.open(fileobj=BytesIO((out / 'export.tgz').read_bytes())) as tar:
        tar.extractall(out / 'export', filter='data')
    meta = {
        'task': task.name,
        'arm': arm.name,
        'repeat': repeat,
        'piloop': run.version if isinstance(run, Piloop) else None,
        'base': base,
        'started_at': started_at.isoformat(),
        'budget_usd': budget,
        'max_hours': hours,
        'stopped': stopped,
    }
    (out / 'run.json').write_text(json.dumps(meta, indent=2))
    archive(out, settings)
    return out


def watch(
    box: Box,
    run: Piloop | Plain,
    arm: PiloopArm | PlainArm,
    budget: float,
    hours: float,
    settings: Settings,
    log: Path,
    started: float,
) -> str:
    """Sample the spend into ``log`` until the budget, the hours or the run's own end, then stop the run. Why."""
    stopped = ''
    with log.open('w') as f:

        def record() -> Sample:
            with tracer.start_as_current_span('evals.spend') as span:
                now = sample(box, arm, settings, (time.monotonic() - started) / 3600)
                span.set_attributes({'usd': round(now.usd, 3), 'hours': round(now.hours, 3)})
            f.write(now.model_dump_json() + '\n')
            f.flush()
            return now

        while not stopped:
            now = record()
            if now.usd >= budget:
                stopped = 'budget'
            elif now.hours >= hours:
                stopped = 'hours'
            else:
                stopped = run.ended(box) or ''
                time.sleep(0 if stopped else settings.poll_seconds)
        with tracer.start_as_current_span('evals.stop', attributes={'reason': stopped}):
            run.stop(box, stopped)
        record()
    return stopped


def archive(out: Path, settings: Settings) -> None:
    """Copy the run's export and meta to ``archive_uri``, when it is set."""
    if not settings.archive_uri:
        return
    bucket, _, prefix = settings.archive_uri.removeprefix('s3://').partition('/')
    s3 = boto3.client('s3')
    for name in ('export.tgz', 'run.json', 'spend.jsonl'):
        s3.upload_file(str(out / name), bucket, f'{prefix}/{out.parent.name}/{out.name}/{name}')
