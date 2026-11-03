"""Run one arm on one task in its own sandbox, within a $ and a time cap, and export what it left.

The driver reads the arm's spend every ``poll_seconds``: the tokens of every agent session in the sandbox, at list
price, plus the sandbox's own CPU and memory. It stops the run at the first of the budget, ``max_hours``, or a
campaign's own stop. The export is ``export/``: the repo as a git bundle, piloop's database for a piloop arm, the
agent session files, and ``spend.jsonl``, beside ``run.json``.
"""

import fcntl
import json
import os
import shlex
import tarfile
import time
import uuid
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import boto3
import tomli_w
from opentelemetry import trace
from pydantic import JsonValue

from evals import daytona
from evals.model import PiloopArm, PlainArm, Resources, Sample, SelfTest, Settings, Task, Tokens
from evals.sandbox import EVALUATIONS, OPT, PILOOP_HOME, WORK, Box, Process, Shell, image
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
    'while true; do piloop serve {listen}>> ' + OUT + '/serve.out 2>&1; code=$?; [ $code -eq 0 ] && break;'
    ' echo "$(date -u +%FT%TZ) exit $code" >> ' + OUT + '/restarts; sleep 5; done'
)
"""Run the daemon in the foreground and start it again after an exit other than 0: one line per restart in
``restarts``. ``piloop stop`` ends it with 0, and the loop with it. ``{listen}`` is its ``--listen``, if any."""
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


def workers(task: Task, arm: PiloopArm, settings: Settings, hours: float) -> int:
    """The runs that a piloop arm keeps open at once (``run_slots``): its workers."""
    return CampaignConfig.model_validate(piloop_config(task, arm, settings, hours)).run_slots


def sandbox(task: Task, arm: PiloopArm, settings: Settings, hours: float) -> tuple[Resources, int]:
    """The resources of a piloop arm's sandbox on Modal, and its daemon's evaluation slots.

    The sandbox grows with the arm's workers to ``worker_cpu`` and ``worker_memory_mb`` each, and never below the
    arm's own ``resources``. Each worker gets ``evaluation_slots_per_worker`` slots, so that a large team's submissions
    do not queue for the evaluator.
    """
    n = workers(task, arm, settings, hours)
    r = arm.resources
    cpu, memory = max(r.cpu, n * settings.worker_cpu), max(r.memory_mb, n * settings.worker_memory_mb)
    return Resources(cpu=cpu, memory_mb=memory, gpus=r.gpus), n * settings.evaluation_slots_per_worker


@dataclass
class Piloop:
    """A piloop campaign in the sandbox, under its own ``PILOOP_HOME``."""

    task: Task
    arm: PiloopArm
    settings: Settings
    hours: float
    limits: dict[str, int] = field(default_factory=dict)
    """Live keys of the daemon, set before the campaign starts: on Modal, its evaluation slots."""
    listen: str | None = None
    """``piloop serve --listen``: the address where workers elsewhere reach it. None: the socket only."""
    ready: Callable[[], None] = lambda: None
    """What runs once the daemon answers and before ``piloop init``, which waits for the baseline's measurement: on
    Daytona, the workers and the evaluators join."""
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
        box.start(SUPERVISE.format(listen=f'--listen {self.listen} ' if self.listen else ''))
        box.run(
            f'for i in $(seq {self.settings.daemon_start_seconds:.0f}); do'
            ' printf %s "$GATEWAY_TOKEN" | piloop secret set GATEWAY_TOKEN 2>/dev/null && exit 0; sleep 1; done; exit 1'
        )
        for key, value in self.limits.items():
            box.run(f'piloop limits set {key}={value} --reason {shlex.quote("evals: sized by its workers")}')
        self.ready()
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
    proc: Process | None = field(default=None, repr=False)

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

    def start(self, box: Shell) -> None:
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


def private_files(task: Task, settings: Settings) -> bytes | None:
    """The task's private split, as a gzipped tar, fetched here, on the driver's machine. None: it has none.

    Raises:
        ValueError: the task has a private split, but sealed storage holds none of its files.
    """
    if not task.private:
        return None
    data = sealed_data(f'{task.name}.private', settings)
    with tarfile.open(fileobj=BytesIO(data)) as tar:
        if not tar.getmembers():
            raise ValueError(f'{task.name} has a private split, and <sealed_uri>/{task.name}.private/ is empty')
    return data


def prepare(box: Shell, task: Task, private: bytes | None = None) -> str:
    """Copy the task's repo, data and private split into ``REPO``, commit it, install its deps. Its commit id."""
    data = f'{OPT}/data/{task.name}'
    unpack = ''
    if private is not None:
        box.write(f'{WORK}/private.tgz', private)
        unpack = f' && tar xzf {WORK}/private.tgz -C {REPO} && rm {WORK}/private.tgz'
    return box.run(
        f'mkdir -p {REPO} {OUT} && cp -r {OPT}/tasks/{task.name}/repo/. {REPO}/'
        f' && if [ -d {data} ]; then cp -r {data}/. {REPO}/; fi{unpack}'
        f' && cd {REPO} && git init -q && git add -A'
        f' && GIT_AUTHOR_DATE={BASE_DATE} GIT_COMMITTER_DATE={BASE_DATE} git commit -qm base'
        ' && if [ -f pyproject.toml ]; then uv sync -q; fi && git rev-parse HEAD',
        timeout=900,
    ).strip()


def selftest(box: Shell, task: Task, settings: Settings) -> SelfTest:
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


def meter(boxes: Sequence[Shell]) -> dict[str, dict[str, dict[str, int]]]:
    """The tokens of the agents' session files in ``boxes``, by harness and model, summed over the boxes."""
    total: dict[str, dict[str, dict[str, int]]] = {}
    for box in boxes:
        box.run(f'python3 {OPT}/meter.py {SESSIONS["claude"]} {SESSIONS["codex"]} {WORK}/meter.json')
        for harness, models in json.loads(box.read(f'{WORK}/meter.json')).items():
            for model, count in models.items():
                into = total.setdefault(harness, {}).setdefault(model, {})
                for k, v in count.items():
                    into[k] = into.get(k, 0) + v
    return total


def sample(boxes: Sequence[Shell], usd_per_hour: float, hours: float) -> Sample:
    """What the run has spent so far: the tokens of the agents in ``boxes`` and their price, and its compute."""
    tokens = meter(boxes)
    counts = [count for models in tokens.values() for count in models.values()]
    summed = Tokens(**{k: sum(c.get(k, 0) for c in counts) for k in Tokens.model_fields})
    return Sample(hours=hours, models_usd=price(tokens), compute_usd=hours * usd_per_hour, tokens=summed)


def compute_rate(r: Resources, settings: Settings) -> float:
    """USD per hour of a Modal sandbox of ``r``."""
    return r.cpu * settings.cpu_usd_per_core_hour + r.memory_mb / 1024 * settings.memory_usd_per_gib_hour


@dataclass
class Fleet:
    """The sandboxes of a run.

    ``control`` holds the repo and the run. The meter reads the agents' session files in ``agents``. ``exported`` are
    the agents' sandboxes other than ``control``, whose files the export takes too.
    """

    control: Shell
    agents: list[Shell]
    usd_per_hour: float
    ids: list[str]
    close: Callable[[], None]
    before_start: Callable[[], None] = lambda: None
    """What runs before the run starts: on Daytona, the daemon's ``config.toml``."""
    exported: list[tuple[str, Shell]] = field(default_factory=list)
    described: dict[str, object] = field(default_factory=dict)


def on_modal(
    task: Task, arm: PiloopArm | PlainArm, settings: Settings, hours: float, env: dict[str, str], name: str
) -> tuple[Fleet, Piloop | Plain]:
    """One Modal sandbox for the whole run, grown with a piloop arm's workers."""
    match arm:
        case PiloopArm():
            r, slots = sandbox(task, arm, settings, hours)
            run: Piloop | Plain = Piloop(
                task, arm, settings, hours, limits={'executor.pools.serve.slots.evaluation': slots}
            )
        case PlainArm():
            r = arm.resources
            run = Plain(task, arm, settings, hours)
    box = Box.create(
        settings,
        image([task], arm),
        cpu=r.cpu,
        memory_mb=r.memory_mb,
        hours=hours + 1,
        network=task.network,
        memory_limit_mb=task.memory_limit_mb,
        gpu=f'{task.gpu}:{r.gpus}' if task.gpu else None,
        cloud=task.cloud,
        env=env,
        run=name,
    )
    fleet = Fleet(box, [box], compute_rate(r, settings), [box.sandbox.object_id], box.close)
    fleet.described = {'platform': 'modal'}
    return fleet, run


AGENT = Resources(cpu=2, memory_mb=8192)
"""On the ``daytona`` platform, the Modal sandbox of each agent, a plain arm's included, whatever the arm asks: its
memory a hard limit, the brief's 8 GiB. Every arm builds and dev-runs on the same box."""


def agent_box(
    task: Task, arm: PiloopArm | PlainArm, settings: Settings, hours: float, env: dict[str, str], run: str
) -> Box:
    """A Modal sandbox of ``AGENT``."""
    return Box.create(
        settings,
        image([task], arm),
        cpu=AGENT.cpu,
        memory_mb=AGENT.memory_mb,
        memory_limit_mb=AGENT.memory_mb,
        hours=hours + 1,
        network=task.network,
        cloud=task.cloud,
        env=env,
        run=run,
    )


def on_daytona(
    task: Task, arm: PiloopArm | PlainArm, settings: Settings, hours: float, env: dict[str, str], name: str
) -> tuple[Fleet, Piloop | Plain]:
    """Each agent in a Modal sandbox of ``AGENT``; a piloop arm's daemon and evaluators on Daytona (``daytona``)."""
    if isinstance(arm, PlainArm):
        only = agent_box(task, arm, settings, hours, env, name)
        fleet = Fleet(only, [only], compute_rate(AGENT, settings), [only.sandbox.object_id], only.close)
        fleet.described = {'platform': 'daytona', 'agents_on': 'modal'}
        return fleet, Plain(task, arm, settings, hours)
    team, plan = daytona.sandboxes(task, arm, workers(task, arm, settings, hours), env, settings, name)
    agents: list[Box] = []

    def close() -> None:
        team.close()
        for a in agents:
            a.close()

    try:
        agents += [agent_box(task, arm, settings, hours, env, f'{name}/worker{i}') for i in range(plan.workers)]
    except Exception:
        close()  # what was made so far: none outlives a run that never started
        raise
    usd = sum(daytona.rate(b.size, settings) for b in team.boxes) + len(agents) * compute_rate(AGENT, settings)
    ids = [b.id for b in team.boxes] + [a.sandbox.object_id for a in agents]
    fleet = Fleet(team.control, list(agents), usd, ids, close)
    fleet.described = daytona.described(plan, team) | {'agents_on': 'modal'}
    sealed = sealed_data(f'{task.name}.campaign', settings) if arm.split == 'sealed' else None
    fleet.before_start = lambda: daytona.configure(team.control)
    fleet.exported = [(f'workers/{i}', a) for i, a in enumerate(agents)]
    run = Piloop(task, arm, settings, hours, listen=f'0.0.0.0:{daytona.API}')
    run.ready = lambda: daytona.enlist(team.control, agents, team.evaluators, sealed, SEALED)
    return fleet, run


def alive(entry: dict[str, JsonValue]) -> bool:
    """Whether a ledger entry still holds its sessions: until its ``until``, or while its driver's ``pid`` lives."""
    if isinstance(until := entry.get('until'), str):
        return datetime.now(UTC) < datetime.fromisoformat(until)
    try:
        os.kill(int(str(entry.get('pid'))), 0)
    except ProcessLookupError, ValueError:
        return False
    except PermissionError:  # another user's process: it lives
        return True
    return True


@contextmanager
def admitted(settings: Settings, run: str, sessions: int) -> Generator[None]:
    """Hold ``sessions`` of the gateway's ``max_sessions`` while the run lives, or refuse the run.

    Each driver on this machine writes an entry in ``session_ledger``, under a lock, and deletes it when its run ends.
    Sessions that no driver here starts, such as another campaign's on the same pool, are an entry with an ``until``,
    written by hand. An entry whose driver died holds nothing.

    Raises:
        ValueError: The run would take the sessions over ``max_sessions``.
    """
    ledger = settings.session_ledger
    ledger.mkdir(parents=True, exist_ok=True)
    mine = ledger / f'{os.getpid()}-{uuid.uuid4().hex[:8]}.json'
    with (ledger / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        held = {f.name: json.loads(f.read_text()) for f in ledger.glob('*.json')}
        live = {name: e for name, e in held.items() if alive(e)}
        taken = sum(int(e['sessions']) for e in live.values())
        if taken + sessions > settings.max_sessions:
            runs = ', '.join(f'{e["run"]} ({e["sessions"]})' for e in live.values())
            raise ValueError(f'{run} needs {sessions} sessions: {taken} of {settings.max_sessions} are taken by {runs}')
        mine.write_text(json.dumps({'run': run, 'sessions': sessions, 'pid': os.getpid()}))
    try:
        yield
    finally:
        mine.unlink(missing_ok=True)


def sessions(task: Task, arm: PiloopArm | PlainArm, settings: Settings, hours: float) -> int:
    """The agent sessions that a run of ``arm`` keeps open at once on the gateway: its workers, or one."""
    return workers(task, arm, settings, hours) if isinstance(arm, PiloopArm) else 1


PLATFORMS = {'modal': on_modal, 'daytona': on_daytona}
"""How a run gets its sandboxes, by ``Settings.platform``."""


def drive(task: Task, arm: PiloopArm | PlainArm, repeat: int, budget: float, hours: float, settings: Settings) -> Path:
    """Run ``arm`` on ``task`` and export it to ``results/<task>/<arm>-<repeat>/``. Its directory."""
    if not settings.gateway or not settings.gateway_token.get_secret_value():
        raise ValueError('set EVALS_GATEWAY and EVALS_GATEWAY_TOKEN: the agents need a model gateway')
    out = settings.results / task.name / f'{arm.name}-{repeat}'
    out.mkdir(parents=True, exist_ok=False)
    name = f'{task.name}/{arm.name}-{repeat}'
    env = (exported(settings, name) if isinstance(arm, PiloopArm) else {}) | arm.env
    env['EVALS_EVAL_LOG'] = f'{OUT}/{EVALUATIONS}'
    private = private_files(task, settings)
    with (
        admitted(settings, name, sessions(task, arm, settings, hours)),
        tracer.start_as_current_span('evals.run', attributes={'evals.run': name, 'budget_usd': budget}) as span,
    ):
        with tracer.start_as_current_span('evals.sandbox'):
            fleet, run = PLATFORMS[settings.platform](task, arm, settings, hours, env, name)
        span.set_attribute('sandboxes', ' '.join(fleet.ids))
        (out / 'sandbox').write_text('\n'.join(fleet.ids))
        control = fleet.control
        base, started_at, stopped = '', datetime.now(UTC), 'error: the run failed before its agents started'
        try:
            with tracer.start_as_current_span('evals.prepare'):
                base = prepare(control, task, private)
            with tracer.start_as_current_span('evals.selftest'):
                (out / 'selftest.json').write_text(selftest(control, task, settings).model_dump_json())
            with tracer.start_as_current_span('evals.start'):
                fleet.before_start()
                run.start(control)
            started_at, started = datetime.now(UTC), time.monotonic()
            stopped = watch(fleet, run, budget, hours, settings, out / 'spend.jsonl', started)
        except Exception as e:
            stopped = f'error: {type(e).__name__}: {e}'[:500]
            raise
        finally:
            span.set_attribute('stopped', stopped)
            try:
                with tracer.start_as_current_span('evals.export'):
                    export(fleet, run, out)
            finally:
                fleet.close()
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
                **fleet.described,
            }
            (out / 'run.json').write_text(json.dumps(meta, indent=2))
            archive(out, settings)
    return out


def export(fleet: Fleet, run: Piloop | Plain, out: Path) -> None:
    """Export the run to ``out/export``.

    The control sandbox gives its repo, its run and its sessions; each other agent's sandbox, its sessions, under its
    own directory.
    """
    control = fleet.control
    run.export(control)
    control.run(
        f'git -C {REPO} bundle create {OUT}/repo.bundle --all'
        f' && tar czf {WORK}/export.tgz --ignore-failed-read -C {WORK} out'
        ' -C /root .claude/projects .codex/sessions',
        timeout=900,
    )
    archives = [('', control.read(f'{WORK}/export.tgz'))]
    for where, box in fleet.exported:
        box.run(
            f'mkdir -p {WORK}/out && tar czf {WORK}/sessions.tgz --ignore-failed-read'
            f' -C /root .claude/projects .codex/sessions -C {WORK} out',
            timeout=900,
        )
        archives.append((where, box.read(f'{WORK}/sessions.tgz')))
    (out / 'export.tgz').write_bytes(archives[0][1])
    for where, data in archives:
        with tarfile.open(fileobj=BytesIO(data)) as tar:
            tar.extractall(out / 'export' / where, filter='data')


def watch(
    fleet: Fleet,
    run: Piloop | Plain,
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
                        now = sample(fleet.agents, fleet.usd_per_hour, (time.monotonic() - started) / 3600)
                    # Modal's link to the sandbox drops now and then, and the meter can be killed (exit 137 when the
                    # sandbox runs out of memory): try again.
                    except (ConnectionError, RuntimeError) as e:
                        span.add_event('evals.sample_retry', {'attempt': attempt, 'error': str(e)})
                        failed = e
                        if fleet.control.ended():
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
                stopped = run.ended(fleet.control) or ''
                time.sleep(0 if stopped else settings.poll_seconds)
        with tracer.start_as_current_span('evals.stop', attributes={'reason': stopped}):
            run.stop(fleet.control, stopped)
        record()
    return stopped


def archive(out: Path, settings: Settings) -> None:
    """Copy the run's export and meta to ``archive_uri``, when it is set."""
    if not settings.archive_uri:
        return
    bucket, _, prefix = settings.archive_uri.removeprefix('s3://').partition('/')
    s3 = boto3.client('s3')
    for name in ('export.tgz', 'run.json', 'spend.jsonl'):
        if (out / name).exists():  # a run that failed before its first sample has no spend
            s3.upload_file(str(out / name), bucket, f'{prefix}/{out.parent.name}/{out.name}/{name}')
