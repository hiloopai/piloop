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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import boto3
import modal
import tomli_w
from opentelemetry import trace
from pydantic import JsonValue

from evals.model import PiloopArm, PlainArm, Resources, Sample, SelfTest, Settings, Task, Tokens
from evals.sandbox import EVALUATIONS, OPT, PILOOP_HOME, WORK, Box, Shell, image
from evals.score import sealed_data
from evals.telemetry import exported, tracer
from piloop.config import CampaignConfig
from piloop.plugins.harness.codex import toml
from piloop.plugins.harness.contract import ModelId, Usage
from piloop.plugins.price_list.genai_prices import GenaiPrices

REPO = f'{WORK}/repo'
SEALED = f'{PILOOP_HOME}/sealed'
"""A campaign's own sealed split: in its home, outside every agent's checkout."""
OUT = f'{WORK}/out'
SESSIONS = {'claude': '/root/.claude/projects', 'codex': '/root/.codex/sessions'}
VENDORS = {'claude': 'anthropic', 'codex': 'openai'}
SUPERVISE = (
    f'while true; do piloop serve >> {OUT}/serve.out 2>&1; code=$?; [ $code -eq 0 ] && break;'
    f' echo "$(date -u +%FT%TZ) exit $code" >> {OUT}/restarts; sleep 5; done'
)
"""Run the daemon in the foreground and start it again after an exit other than 0: one line per restart in
``restarts``. ``piloop stop`` ends it with 0, and the loop with it."""
HALT = 'halt'
INSTRUCTION = 'worker-instruction.md'
"""The file of an arm's instruction in the campaign's checkout, out of git: the implementer's ``addendum``."""
CHARTER = 'charter-{role}.md'
"""The file of a role's charter in the campaign's checkout, out of git: that role's ``addendum``."""
"""A file in a run's directory that stops it at the next sample, with its text as the reason."""
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
    """The ``-c`` keys of every Codex agent: the gateway, and the summaries of its reasoning.

    The gateway takes the token from ``GATEWAY_TOKEN``. The session file keeps the summaries, so that a run's reasoning
    can be read afterwards.
    """
    provider = {'name': 'gateway', 'base_url': f'{settings.gateway}/v1', 'env_key': 'GATEWAY_TOKEN'}
    return {
        'model_provider': 'gateway',
        'model_providers': {'gateway': {**provider, 'wire_api': 'responses'}},
        'model_reasoning_summary': settings.codex_reasoning_summary,
    }


def charters(task: Task, arm: PiloopArm) -> dict[str, str]:
    """The charter of each role of ``arm`` on ``task``. An arm with charters for other tasks only cannot run it."""
    if arm.charters and task.name not in arm.charters:
        raise ValueError(f'{arm.name} has charters for {", ".join(arm.charters)}, not for {task.name}')
    return arm.charters.get(task.name, {})


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
    nice: list[JsonValue] = ['nice', '-n', str(settings.evaluation_nice)]
    evaluate = task.piloop.get('evaluate')
    if isinstance(evaluate, list):
        own['evaluate'] = [*nice, *evaluate]
    if arm.instruction:
        own = merge(own, {'plugins': {'role': {'implementer': {'addendum': INSTRUCTION}}}})
    for role in charters(task, arm):
        own = merge(own, {'plugins': {'role': {role: {'addendum': CHARTER.format(role=role)}}}})
    match arm.split:
        case 'sealed':
            script = f'{OPT}/campaign_sealed.py'
            sealed: list[JsonValue] = [*nice, 'python3', script, f'{OPT}/tasks/{task.name}', SEALED, task.direction]
            evaluator: dict[str, JsonValue] = {'dev': [*nice, *task.dev], 'sealed': sealed}
            own['evaluator'] = evaluator
        case 'visible':
            pass
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


def sandbox(task: Task, arm: PiloopArm, settings: Settings, hours: float) -> tuple[Resources, int]:
    """The resources of a piloop arm's sandbox, and its daemon's evaluation slots.

    The sandbox grows with the arm's workers, the runs that it keeps open at once (``run_slots``), to ``worker_cpu``
    and ``worker_memory_mb`` each, and never below the arm's own ``resources``. Each worker gets
    ``evaluation_slots_per_worker`` slots, so that a large team's submissions do not queue for the evaluator.
    """
    workers = CampaignConfig.model_validate(piloop_config(task, arm, settings, hours)).run_slots
    r = arm.resources
    cpu, memory = max(r.cpu, workers * settings.worker_cpu), max(r.memory_mb, workers * settings.worker_memory_mb)
    return Resources(cpu=cpu, memory_mb=memory, gpus=r.gpus), workers * settings.evaluation_slots_per_worker


@dataclass
class Piloop:
    """A piloop campaign in the sandbox, under its own ``PILOOP_HOME``."""

    task: Task
    arm: PiloopArm
    settings: Settings
    hours: float
    slots: int
    """The daemon's evaluation slots (``executor.pools.serve.slots.evaluation``), set before the campaign starts."""
    version: str = ''

    @property
    def name(self) -> str:
        return str(self.task.piloop['name'])

    def sql(self, box: Shell, query: str) -> str:
        return box.run(f'sqlite3 {PILOOP_HOME}/piloop.db {shlex.quote(query)}').strip()

    def start(self, box: Shell) -> None:
        if self.arm.split == 'sealed':
            box.write(f'{WORK}/campaign.tgz', sealed_data(f'{self.task.name}.campaign', self.settings))
            box.run(f'mkdir -p {SEALED} && tar xzf {WORK}/campaign.tgz -C {SEALED} && rm {WORK}/campaign.tgz')
        config = piloop_config(self.task, self.arm, self.settings, self.hours)
        if self.arm.instruction:
            box.write(f'{REPO}/{INSTRUCTION}', self.arm.instruction.encode())
            box.run(f'cd {REPO} && echo {INSTRUCTION} >> .git/info/exclude')
        for role, text in charters(self.task, self.arm).items():
            box.write(f'{REPO}/{CHARTER.format(role=role)}', text.encode())
            box.run(f'cd {REPO} && echo {CHARTER.format(role=role)} >> .git/info/exclude')
        box.write(f'{REPO}/piloop.toml', tomli_w.dumps(config).encode())
        box.run(f'cd {REPO} && echo piloop.toml >> .git/info/exclude')
        box.start(SUPERVISE)
        box.run(
            f'for i in $(seq {self.settings.daemon_start_seconds:.0f}); do'
            ' printf %s "$GATEWAY_TOKEN" | piloop secret set GATEWAY_TOKEN 2>/dev/null && exit 0; sleep 1; done; exit 1'
        )
        reason = shlex.quote(f'evals: {self.settings.evaluation_slots_per_worker} per worker')
        box.run(f'piloop limits set executor.pools.serve.slots.evaluation={self.slots} --reason {reason}')
        box.run(f'cd {REPO} && piloop init . > {OUT}/init.log 2>&1', timeout=1800)
        self.version = box.run('piloop --version').strip()
        trace.get_current_span().set_attribute('piloop.version', self.version)

    def ended(self, box: Shell) -> str | None:
        """The campaign's own stop reason, once it stopped."""
        status = self.sql(
            box, f"select status || '|' || coalesce(stopped_reason, '') from campaigns where name = '{self.name}'"
        )
        state, _, reason = status.partition('|')
        return None if state == 'active' else f'campaign: {reason}'

    def stop(self, box: Shell, reason: str) -> None:
        """Stop at once and give the daemon ``stop_grace_minutes`` to score what the agents committed."""
        box.run(f'piloop campaign stop {self.name} --kill --reason {shlex.quote(reason)} || true')
        deadline = time.monotonic() + self.settings.stop_grace_minutes * 60
        pending = "select count(*) from runs where status in ('queued', 'running') or scoring"
        while time.monotonic() < deadline and self.sql(box, pending) != '0':
            time.sleep(self.settings.poll_seconds / 4)

    def export(self, box: Shell) -> None:
        box.run(f'sqlite3 {PILOOP_HOME}/piloop.db ".backup {OUT}/piloop.db" && piloop stop || true')


@dataclass
class Plain:
    """One agent session, resumed with ``CONTINUE_PROMPT`` each time it ends before the run does.

    The resumes run in a loop inside the sandbox, so a session that ends is resumed at once, not at the next sample.
    """

    task: Task
    arm: PlainArm
    settings: Settings
    hours: float
    proc: modal.container_process.ContainerProcess[str] | None = field(default=None, repr=False)

    def command(self, prompt: str, session: str | None) -> str:
        """The shell command of the first session (``session`` None), or of a resume of ``session``, a shell word."""
        agent = self.arm.agent
        match agent.harness:
            case 'codex':
                flags = ['--json', '--dangerously-bypass-approvals-and-sandbox', '--skip-git-repo-check']
                flags += ['--model', agent.model, '-c', f'model_reasoning_effort="{self.arm.effort}"']
                for key, value in codex_gateway(self.settings).items():
                    flags += ['-c', f'{key}={toml(value)}']
                head = shlex.join(['codex', 'exec', *(['resume'] if session else []), *flags])
                return f'{head} {session or ""} {shlex.quote(prompt)}'
            case 'claude':
                env = f'ANTHROPIC_BASE_URL={shlex.quote(self.settings.gateway)} ANTHROPIC_AUTH_TOKEN="$GATEWAY_TOKEN"'
                flags = ['--model', agent.model, '--effort', self.arm.effort, '--dangerously-skip-permissions']
                head = shlex.join(['claude', '-p', '--output-format', 'stream-json', '--verbose', *flags])
                return f'{env} {head} {"--resume " + session if session else "--session-id $session"} {shlex.quote(prompt)}'

    def script(self, prompt: str) -> str:
        """The first session, then a resume of it each time it ends, until the driver stops the sandbox.

        A resume that fails at once is retried after a second, so that a broken session does not spin.
        """
        first = f'{OUT}/session-1.jsonl'
        match self.arm.agent.harness:
            case 'codex':
                name = f'session=$(grep -m 1 -o \'"thread_id": *"[^"]*\' {first} | cut -d \'"\' -f 4)'
            case 'claude':
                name = 'true'
        return (
            f'cd {REPO} && session=$(cat /proc/sys/kernel/random/uuid)'
            f' && {self.command(prompt, None)} < /dev/null > {first} 2>&1; {name};'
            ' [ -n "$session" ] || { echo "the first session named no session id" >&2; exit 1; };'
            ' n=1; while true; do n=$((n + 1));'
            f' {self.command(CONTINUE_PROMPT, "$session")} < /dev/null > {OUT}/session-$n.jsonl 2>&1; sleep 1; done'
        )

    def start(self, box: Box) -> None:
        prompt = PLAIN_PROMPT.format(dev=shlex.join(self.task.dev), hours=self.hours)
        self.proc = box.start(self.script(f'{prompt}\n\n{self.arm.instruction}' if self.arm.instruction else prompt))

    def ended(self, box: Shell) -> str | None:
        """Only when the loop of sessions failed: a session that ends is resumed inside it."""
        if self.proc is not None and self.proc.poll() is not None:
            return f'the sessions ended: {self.proc.stderr.read()[-500:]}'
        return None

    def stop(self, box: Shell, reason: str) -> None:
        box.run('pkill -f "[w]hile true; do n=" || true; pkill -f "[c]odex exec|[c]laude -p" || true')

    def export(self, box: Shell) -> None:
        box.run(f'ls {OUT}/session-*.jsonl | wc -l > {OUT}/sessions')


def prepare(box: Box, task: Task) -> str:
    """Copy the task's repo and data into ``REPO``, make the starting commit, install its deps. Its commit id."""
    data = f'{OPT}/data/{task.name}'
    return box.run(
        f'mkdir -p {REPO} {OUT} && cp -r {OPT}/tasks/{task.name}/repo/. {REPO}/'
        f' && if [ -d {data} ]; then cp -r {data}/. {REPO}/; fi'
        f' && cd {REPO} && git init -q && git add -A'
        f' && GIT_AUTHOR_DATE={BASE_DATE} GIT_COMMITTER_DATE={BASE_DATE} git commit -qm base'
        ' && if [ -f pyproject.toml ]; then uv sync -q; fi && git rev-parse HEAD',
        timeout=900,
    ).strip()


def selftest(box: Box, task: Task, settings: Settings) -> SelfTest:
    """Evaluate the starting repo ``selftest_runs`` times with the task's dev command, and read the CPU.

    It runs before the agents start, and outside the evaluations log. A base that fails raises: no run starts on a
    broken evaluator.
    """
    cpu = box.run(f'python3 -c "import sys; sys.path.insert(0, {OPT!r}); import scorer; print(scorer.cpu())"').strip()
    timeout = task.piloop.get('evaluate_timeout')
    values = []
    for _ in range(settings.selftest_runs):
        out = box.run(
            f'cd {REPO} && env -u EVALS_EVAL_LOG {shlex.join(task.dev)}',
            timeout=float(timeout) if isinstance(timeout, int | float) else None,
        )
        values.append(float(json.loads(out.strip().splitlines()[-1])['metrics'][task.metric]))
    return SelfTest(cpu=cpu, values=values)


def sample(box: Shell, r: Resources, settings: Settings, hours: float) -> Sample:
    """What the run has spent so far, in a sandbox of ``r``: its models' tokens and their price, and its compute."""
    box.run(f'python3 {OPT}/meter.py {SESSIONS["claude"]} {SESSIONS["codex"]} {WORK}/meter.json')
    tokens = json.loads(box.read(f'{WORK}/meter.json'))
    counts = [count for models in tokens.values() for count in models.values()]
    summed = Tokens(**{k: sum(c[k] for c in counts) for k in Tokens.model_fields})
    compute = hours * (r.cpu * settings.cpu_usd_per_core_hour + r.memory_mb / 1024 * settings.memory_usd_per_gib_hour)
    return Sample(hours=hours, models_usd=price(tokens), compute_usd=compute, tokens=summed)


def drive(task: Task, arm: PiloopArm | PlainArm, repeat: int, budget: float, hours: float, settings: Settings) -> Path:
    """Run ``arm`` on ``task`` and export it to ``results/<task>/<arm>-<repeat>/``. Its directory."""
    if not settings.gateway or not settings.gateway_token.get_secret_value():
        raise ValueError('set EVALS_GATEWAY and EVALS_GATEWAY_TOKEN: the agents need a model gateway')
    out = settings.results / task.name / f'{arm.name}-{repeat}'
    out.mkdir(parents=True, exist_ok=False)
    name = f'{task.name}/{arm.name}-{repeat}'
    match arm:
        case PiloopArm():
            r, slots = sandbox(task, arm, settings, hours)
            run: Piloop | Plain = Piloop(task, arm, settings, hours, slots=slots)
            env = exported(settings, name) | arm.env
        case PlainArm():
            r = arm.resources
            run = Plain(task, arm, settings, hours)
            env = dict(arm.env)
    env['EVALS_EVAL_LOG'] = f'{OUT}/{EVALUATIONS}'
    with tracer.start_as_current_span('evals.run', attributes={'evals.run': name, 'budget_usd': budget}) as span:
        with tracer.start_as_current_span('evals.sandbox'):
            img = image([task], arm)
            gpu = f'{task.gpu}:{r.gpus}' if task.gpu else None
            box = Box.create(
                settings,
                img,
                cpu=r.cpu,
                memory_mb=r.memory_mb,
                hours=hours + 1,
                network=True,
                gpu=gpu,
                cloud=task.cloud,
                env=env,
                run=name,
            )
        span.set_attribute('modal.sandbox', box.sandbox.object_id)
        (out / 'sandbox').write_text(box.sandbox.object_id)
        base, started_at, stopped = '', datetime.now(UTC), 'error: the run failed before its agents started'
        try:
            with tracer.start_as_current_span('evals.prepare'):
                base = prepare(box, task)
            with tracer.start_as_current_span('evals.selftest'):
                (out / 'selftest.json').write_text(selftest(box, task, settings).model_dump_json())
            with tracer.start_as_current_span('evals.start'):
                run.start(box)
            started_at, started = datetime.now(UTC), time.monotonic()
            stopped = watch(box, run, r, budget, hours, settings, out / 'spend.jsonl', started)
        except Exception as e:
            stopped = f'error: {type(e).__name__}: {e}'[:500]
            raise
        finally:
            span.set_attribute('stopped', stopped)
            try:
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
                'supervised': isinstance(run, Piloop),
            }
            (out / 'run.json').write_text(json.dumps(meta, indent=2))
            archive(out, settings)
    return out


def watch(
    box: Shell,
    run: Piloop | Plain,
    r: Resources,
    budget: float,
    hours: float,
    settings: Settings,
    log: Path,
    started: float,
) -> str:
    """Sample the spend into ``log`` until a stop, then stop the run. Why it stopped.

    A stop is a ``HALT`` file beside ``log``, the budget, the hours, the run's own end, or a spend sample that failed
    ``sample_attempts`` times: then the run stops with that error, and the driver still exports it.
    """
    stopped = ''
    with log.open('w') as f:

        def record() -> Sample | RuntimeError | ConnectionError:
            """The spend now, written to ``log``; or the error of the last attempt, when every attempt failed."""
            with tracer.start_as_current_span('evals.spend') as span:
                failed: RuntimeError | ConnectionError = RuntimeError('no attempt: sample_attempts is 0')
                for attempt in range(settings.sample_attempts):
                    try:
                        now = sample(box, r, settings, (time.monotonic() - started) / 3600)
                    # Modal's link to the sandbox drops now and then, and the meter can be killed (exit 137 when the
                    # sandbox runs out of memory): try again.
                    except (ConnectionError, RuntimeError) as e:
                        span.add_event('evals.sample_retry', {'attempt': attempt, 'error': str(e)})
                        failed = e
                        if box.ended():
                            break
                        time.sleep(settings.poll_seconds / 4)
                    else:
                        span.set_attributes({'usd': round(now.usd, 3), 'hours': round(now.hours, 3)})
                        f.write(now.model_dump_json() + '\n')
                        f.flush()
                        return now
                span.set_attribute('error', str(failed)[:500])
                return failed

        while not stopped:
            now = record()
            halt = log.parent / HALT
            if not isinstance(now, Sample):
                stopped = f'sample failed: {now}'[:500]
            elif halt.exists():
                stopped = f'halted: {halt.read_text().strip()}'
            elif now.usd >= budget:
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
