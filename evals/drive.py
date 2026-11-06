"""Run one arm on one task in its own sandbox, within a $ and a time cap, and export what it left.

The driver reads the arm's spend every ``poll_seconds``: the tokens of every agent session in the sandbox, at list
price, plus the sandbox's own CPU and memory. It stops the run at the first of the budget, ``max_hours``, or a
campaign's own stop. The export is ``export/``: the repo as a git bundle, piloop's database for a piloop arm, the
agent session files, and ``spend.jsonl``, beside ``run.json``.
"""

import contextlib
import fcntl
import gzip
import hashlib
import json
import os
import shlex
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import boto3
import tomli_w
from modal.config import config as modal_config
from opentelemetry import trace
from pydantic import JsonValue

from evals import daytona
from evals.model import PiloopArm, PlainArm, Resources, Sample, SelfTest, Settings, Task, Tokens
from evals.sandbox import (
    EVALUATIONS,
    OPT,
    PILOOP_HOME,
    WORK,
    Box,
    Copy,
    Process,
    Shell,
    Step,
    built,
    image,
    network_policy,
    recipe,
    tagged,
)
from evals.score import sealed_data
from evals.telemetry import exported, tracer
from piloop.config import CampaignConfig
from piloop.plugins.executor.kit.scaling import OWNER_FILE
from piloop.plugins.harness.claude import Claude
from piloop.plugins.harness.codex import Codex, toml
from piloop.plugins.harness.contract import Harness, Usage
from piloop.plugins.harness.pi import Pi
from piloop.plugins.price_list.genai_prices import GenaiPrices
from piloop.plugins.provisioner.modal import OWNER as MODAL_OWNER, POOL as MODAL_POOL
from piloop.plugins.role.orchestrator import OrchestratorRole

REPO = f'{WORK}/repo'
SEALED = f'{PILOOP_HOME}/sealed'
"""A campaign's own sealed split: in its home, outside every agent's checkout."""
OUT = f'{WORK}/out'
HOME = '/root'
SESSIONS = {'claude': '.claude/projects', 'codex': '.codex/sessions', 'pi': '.pi/agent/sessions'}
"""Where each harness keeps its session files, under ``HOME``: the meter reads them, the export takes them."""
HARNESSES: dict[str, Harness] = {'claude': Claude(), 'codex': Codex(), 'pi': Pi()}
"""piloop's own harnesses, which name the vendor of each model that the meter counts."""
type Counts = dict[str, dict[str, dict[str, int]]]
"""The meter's tokens: by harness, by model, by kind."""
CREDENTIALS = f'{PILOOP_HOME}/credentials.env'
"""On the control sandbox, what its daemon reads: the gateway's token, and the keys of the platforms that its
provisioners start the team on. Never in its environment, which Daytona's API shows."""
SUPERVISE = (
    f'. {CREDENTIALS} && while true; do piloop serve --listen {{listen}} >> {OUT}/serve.out 2>&1; code=$?;'
    f' [ $code -eq 0 ] && break; echo "$(date -u +%FT%TZ) exit $code" >> {OUT}/restarts; sleep 5; done'
)
"""Run the daemon in the foreground and start it again after an exit other than 0: one line per restart in
``restarts``. ``piloop stop`` ends it with 0, and the loop with it. ``{listen}`` is its ``--listen``."""
DRAIN_SECONDS = 600
"""How long the team's workers get to end once the run is over, before the daemon stops: a worker that is left
ends at its provisioner's TTL."""
HEADERS = 'OTEL_EXPORTER_OTLP_HEADERS'
"""The collector's credentials, which only the control sandbox gets (``CREDENTIALS``)."""
HALT = 'halt'
"""A file in a run's directory that stops it at the next sample, with its text as the reason."""
INSTRUCTION = 'worker-instruction.md'
"""The file of an arm's instruction in the campaign's checkout, out of git: the implementer's ``addendum``."""
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


PI_ENV = {'PI_OFFLINE': '1'}
"""Every pi agent: no model catalog from pi.dev or version check at its start, so that a run's models stay those of
its image, and a sandbox that reaches the gateway alone starts as one that reaches all."""


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
            case 'pi':  # the gateway's URL is in its image (``sandbox.PI_MODELS``)
                harness['pi'] = {
                    'model': agent.model,
                    'user_config': 'ignore',
                    'env': {'ANTHROPIC_AUTH_TOKEN': {'secret': 'GATEWAY_TOKEN'}, **PI_ENV},
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
    match arm.split:
        case 'sealed':
            script = f'{OPT}/campaign_sealed.py'
            sealed: list[JsonValue] = [*nice, 'python3', script, f'{OPT}/tasks/{task.name}', SEALED, task.direction]
            evaluator: dict[str, JsonValue] = {'dev': [*nice, *task.dev], 'sealed': sealed}
            own['evaluator'] = evaluator
        case 'visible':
            pass
    return merge(merge(task.piloop, own), arm.overlay)


def price(tokens: Counts) -> float:
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
            cost = prices.usd(HARNESSES[harness].model_id(model), usage)
            if cost is None:
                raise ValueError(f'no list price for {harness} model {model!r}')
            usd += cost
    return usd


def run_slots(task: Task, arm: PiloopArm, settings: Settings, hours: float) -> int:
    """The runs that a piloop arm keeps open at once: each gets an evaluator."""
    return CampaignConfig.model_validate(piloop_config(task, arm, settings, hours)).run_slots


def workers(task: Task, arm: PiloopArm, settings: Settings, hours: float) -> int:
    """The agents that a piloop arm keeps open at once, each in a box of its own.

    One for each run slot (``run_slots``), plus each agent of an orchestrator that its triggers start: without a box of
    its own, it would wait for a run to free one and then take it from the implementers.
    """
    cfg = CampaignConfig.model_validate(piloop_config(task, arm, settings, hours))
    lead = sum(
        r.max_concurrent for r in cfg.roles.starting().values() if isinstance(r, OrchestratorRole) and r.triggers
    )
    return cfg.run_slots + lead


@dataclass
class Piloop:
    """A piloop campaign in the sandbox, under its own ``PILOOP_HOME``."""

    task: Task
    arm: PiloopArm
    settings: Settings
    hours: float
    served: bool = False
    """Its daemon started, and with it the team that ``export`` drains."""
    version: str = ''

    @property
    def name(self) -> str:
        return str(self.task.piloop['name'])

    def sql(self, box: Shell, query: str) -> str:
        return box.run(f'sqlite3 {PILOOP_HOME}/piloop.db {shlex.quote(query)}').strip()

    def start(self, box: Shell) -> None:
        """Start the daemon, whose provisioners start the team, and register the campaign once its baseline is in."""
        config = piloop_config(self.task, self.arm, self.settings, self.hours)
        if self.arm.instruction:
            box.write(f'{REPO}/{INSTRUCTION}', self.arm.instruction.encode())
            box.run(f'cd {REPO} && echo {INSTRUCTION} >> .git/info/exclude')
        box.write(f'{REPO}/piloop.toml', tomli_w.dumps(config).encode())
        box.run(f'cd {REPO} && echo piloop.toml >> .git/info/exclude')
        box.start(SUPERVISE.format(listen=f'0.0.0.0:{daytona.API}'))
        box.run(
            f'. {CREDENTIALS} && for i in $(seq {self.settings.daemon_start_seconds:.0f}); do'
            ' printf %s "$GATEWAY_TOKEN" | piloop secret set GATEWAY_TOKEN 2>/dev/null && exit 0; sleep 1; done; exit 1'
        )
        self.served = True
        box.start(
            f'piloop cas serve --listen 0.0.0.0:{daytona.CAS} >> {OUT}/cas.out 2>&1'
        )  # the daemon made its database
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
        """Drain the team, keep the database, and stop the daemon.

        Raises:
            RuntimeError: a sandbox of the team did not end in ``DRAIN_SECONDS``; it ends at its provisioner's TTL.
        """
        left = '0'
        if self.served:
            reason = shlex.quote('evals: the run is over')
            for pool in (daytona.AGENTS, daytona.EVAL):
                for key in ('min', 'max'):  # min first: a max below min is refused
                    box.run(f'piloop limits set executor.pools.{pool}.provisioner.{key}=0 --reason {reason}')
            deadline = time.monotonic() + DRAIN_SECONDS
            query = shlex.quote("select count(*) from worker_instances where state != 'ended'")
            while (left := box.run(f'sqlite3 {PILOOP_HOME}/piloop.db {query}').strip()) != '0':
                if time.monotonic() >= deadline:
                    break
                time.sleep(self.settings.poll_seconds / 4)
        box.run(f'sqlite3 {PILOOP_HOME}/piloop.db ".backup {OUT}/piloop.db" && piloop stop || true')
        if left != '0':
            raise RuntimeError(f'{left} of the team did not end in {DRAIN_SECONDS} s: they end at their TTL')


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
            case 'pi':  # --session-id continues the session once it exists
                env = ' '.join([*(f'{k}={v}' for k, v in PI_ENV.items()), 'ANTHROPIC_AUTH_TOKEN="$GATEWAY_TOKEN"'])
                flags = ['--model', agent.model, '--thinking', self.arm.effort, '--approve', '--no-extensions']
                head = shlex.join(['pi', '-p', '--mode', 'json', *Pi().isolation(), *flags])
                return f'{env} {head} --session-id {session or "$session"} {shlex.quote(prompt)}'

    def script(self, prompt: str) -> str:
        """The first session, then a resume of it each time it ends, until the driver stops the sandbox.

        A resume that fails at once is retried after a second, so that a broken session does not spin.
        """
        first = f'{OUT}/session-1.jsonl'
        match self.arm.agent.harness:
            case 'codex':
                name = f'session=$(grep -m 1 -o \'"thread_id": *"[^"]*\' {first} | cut -d \'"\' -f 4)'
            case 'claude' | 'pi':
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
        box.run('pkill -f "[w]hile true; do n=" || true; pkill -f "[c]odex exec|[c]laude -p|[p]i -p" || true')

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


def meter(box: Shell) -> dict[str, dict[str, dict[str, int]]]:
    """The tokens of the agents' session files in ``box``, by harness and model."""
    roots = ' '.join(f'{harness}={HOME}/{path}' for harness, path in SESSIONS.items())
    box.run(f'mkdir -p {WORK} && python3 {OPT}/meter.py {WORK}/meter.json {roots}')
    return json.loads(box.read(f'{WORK}/meter.json'))


def sample(boxes: Mapping[str, Shell], seen: dict[str, Counts], usd_per_hour: float, hours: float) -> Sample:
    """What the run has spent so far: the tokens of its agents and their price, and its compute.

    ``seen`` keeps each box's last count, by its id, so that a box that ended still counts.
    """
    for name, box in boxes.items():
        seen[name] = meter(box)
    tokens: Counts = {}
    for counted in seen.values():
        for harness, models in counted.items():
            for model, count in models.items():
                into = tokens.setdefault(harness, {}).setdefault(model, {})
                for k, v in count.items():
                    into[k] = into.get(k, 0) + v
    counts = [count for models in tokens.values() for count in models.values()]
    summed = Tokens(**{k: sum(c.get(k, 0) for c in counts) for k in Tokens.model_fields})
    return Sample(hours=hours, models_usd=price(tokens), compute_usd=hours * usd_per_hour, tokens=summed)


def compute_rate(r: Resources, settings: Settings) -> float:
    """USD per hour of a Modal sandbox of ``r``."""
    return r.cpu * settings.cpu_usd_per_core_hour + r.memory_mb / 1024 * settings.memory_usd_per_gib_hour


@dataclass
class Fleet:
    """The sandboxes of a run.

    ``control`` holds the repo and the run. ``agents`` lists the sandboxes of its agents now, by id, ``control`` among
    them for a plain arm: the meter reads their session files, and the export takes those of each but ``control``.
    """

    control: Shell
    agents: Callable[[], Mapping[str, Shell]]
    usd_per_hour: float
    ids: list[str]
    close: Callable[[], None]
    described: dict[str, object] = field(default_factory=dict)


def on_modal(
    task: Task, arm: PlainArm, settings: Settings, hours: float, env: dict[str, str], name: str
) -> tuple[Fleet, Plain]:
    """One Modal sandbox of the arm's ``resources`` for the plain session."""
    r = arm.resources
    box = Box.create(
        settings,
        image([task], arm, settings.gateway),
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
    fleet = Fleet(
        box, lambda: {box.sandbox.object_id: box}, compute_rate(r, settings), [box.sandbox.object_id], box.close
    )
    fleet.described = {'platform': 'modal'}
    return fleet, Plain(task, arm, settings, hours)


AGENT = Resources(cpu=2, memory_mb=8192)
"""The Modal sandbox of each agent of a piloop arm, and of a plain arm on the ``daytona`` platform, whatever the arm
asks: 8 GiB asked for, the brief's, and a hard limit only when the task has one (``memory_limit_mb``). On Modal a box
over its hard limit dies with every session in it, so the brief's 8 GiB binds where it is measured: the evaluator caps
each codec's address space. Every arm builds and dev-runs on the same box."""


def agent_box(task: Task, arm: PlainArm, settings: Settings, hours: float, env: dict[str, str], run: str) -> Box:
    """A Modal sandbox of ``AGENT``."""
    return Box.create(
        settings,
        image([task], arm, settings.gateway),
        cpu=AGENT.cpu,
        memory_mb=task.memory_limit_mb or AGENT.memory_mb,
        memory_limit_mb=task.memory_limit_mb,
        hours=hours + 1,
        network=task.network,
        cloud=task.cloud,
        env=env,
        run=run,
    )


def on_daytona(
    task: Task, arm: PlainArm, settings: Settings, hours: float, env: dict[str, str], name: str
) -> tuple[Fleet, Plain]:
    """The plain session in a Modal sandbox of ``AGENT``, as each agent of a piloop arm."""
    only = agent_box(task, arm, settings, hours, env, name)
    ids = [only.sandbox.object_id]
    fleet = Fleet(only, lambda: {ids[0]: only}, compute_rate(AGENT, settings), ids, only.close)
    fleet.described = {'platform': 'daytona', 'agents_on': 'modal'}
    return fleet, Plain(task, arm, settings, hours)


def sealed_steps(task: Task, arm: PiloopArm, settings: Settings) -> list[Step]:
    """The campaign's sealed split as a copy into ``SEALED``, for the evaluators' image: no agent's has it.

    It is unpacked here, under a directory named by its content, so that its image is the same for the same split: the
    content of the tar, not of its gzip stream, which holds the time it was made.
    """
    if arm.split != 'sealed':
        return []
    data = sealed_data(f'{task.name}.campaign', settings)
    here = (
        Path(tempfile.gettempdir())
        / 'evals-sealed'
        / hashlib.sha256(gzip.decompress(data)).hexdigest()[:16]
        / f'{task.name}.campaign'
    )
    if not here.is_dir():
        unpacked = here.with_name(f'{here.name}.{uuid.uuid4().hex}')
        with tarfile.open(fileobj=BytesIO(data)) as tar:
            tar.extractall(unpacked, filter='data')
        with contextlib.suppress(OSError):  # another driver unpacked the same split first
            unpacked.rename(here)
    return [Copy(here, SEALED)]


def credentials(settings: Settings) -> bytes:
    """The control sandbox's ``CREDENTIALS``: shell lines that export them.

    Raises:
        ValueError: Modal's token or Daytona's key is missing, which the team's provisioners need.
    """
    values = {
        'GATEWAY_TOKEN': settings.gateway_token.get_secret_value(),
        'MODAL_TOKEN_ID': str(modal_config.get('token_id') or ''),
        'MODAL_TOKEN_SECRET': str(modal_config.get('token_secret') or ''),
        'DAYTONA_API_KEY': settings.daytona_api_key.get_secret_value(),
    }
    if missing := [k for k, v in values.items() if not v]:
        raise ValueError(f"a piloop arm's team needs {', '.join(missing)}: `modal token new`, EVALS_DAYTONA_API_KEY")
    if settings.otlp_headers is not None:
        values[HEADERS] = settings.otlp_headers.get_secret_value()
    return ''.join(f'export {k}={shlex.quote(v)}\n' for k, v in values.items()).encode()


def present(table: dict[str, JsonValue | None]) -> dict[str, JsonValue]:
    """``table`` without its unset keys, which TOML cannot hold."""
    return {k: v for k, v in table.items() if v is not None}


def on_team(
    task: Task, arm: PiloopArm, settings: Settings, hours: float, env: dict[str, str], name: str
) -> tuple[Fleet, Piloop]:
    """A piloop arm's team: its daemon on a Daytona control sandbox (``daytona``), whose provisioners start the rest.

    Each agent's worker is a Modal sandbox of ``AGENT``, which reaches what the task's ``network`` allows and the
    control sandbox. Each evaluator runs one evaluation at a time where the task is scored (``Task.scorer``): a Daytona
    sandbox of ``daytona.EVALUATOR``, or a Modal one of the scorer's size. The driver starts only the control sandbox:
    the meter and the export find the agents' sandboxes by their provisioner's tags.
    """
    cloud = daytona.client(settings)
    steps = recipe([task], arm, settings.gateway)
    labels = {'purpose': 'piloop-evals', 'run': name.replace('/', '_')}
    control = daytona.control(cloud, steps, {k: v for k, v in env.items() if k != HEADERS}, labels, hours)
    try:
        api, cas = control.preview(daytona.API), control.preview(daytona.CAS)
        own: list[JsonValue] = [h for h in (urlsplit(api).hostname, urlsplit(cas).hostname) if h]
        reach = network_policy(settings, task.network).outbound_domain_allowlist
        n = workers(task, arm, settings, hours)
        e = daytona.evaluators(run_slots(task, arm, settings, hours), settings)
        # Spans come from the daemon alone: a worker without the collector's credentials would be refused.
        team_env: dict[str, JsonValue] = {k: v for k, v in env.items() if not k.startswith('OTEL_')}
        team_env['PILOOP_PLUGINS'] = daytona.worker_plugins(cas)
        on_modal_: dict[str, JsonValue] = {
            'provider': 'modal',
            'app': settings.modal_app,
            'environment': settings.modal_environment,
        }
        agents = present(
            on_modal_
            | {
                'image_id': built(settings, steps),
                'cpu': AGENT.cpu,
                'memory_mb': task.memory_limit_mb or AGENT.memory_mb,
                'memory_limit_mb': task.memory_limit_mb,
                'gpu': task.gpu,
                'cloud': task.cloud,
                'outbound_domains': None if reach is None else [*reach, *own],
                'timeout_hours': min(hours + 1, 24),
                'env': team_env,
            }
        )
        judged = [*steps, *sealed_steps(task, arm, settings)]
        evaluate: dict[str, JsonValue]
        match task.scorer:
            case 'daytona':
                evaluate = {
                    'provider': 'daytona',
                    'snapshot': daytona.snapshot(cloud, judged, daytona.EVALUATOR),
                    'ttl_hours': hours + 1,
                    'env': team_env,
                }
                evaluator_rate = daytona.rate(daytona.EVALUATOR, settings)
            case 'modal':
                memory = max(settings.score_memory_mb, task.memory_limit_mb or 0)
                evaluate = present(
                    on_modal_
                    | {
                        'image_id': built(settings, judged),
                        'cpu': settings.score_cpu,
                        'memory_mb': memory,
                        'memory_limit_mb': task.memory_limit_mb,
                        'gpu': task.gpu,
                        'cloud': task.cloud,
                        'outbound_domains': own,
                        'timeout_hours': min(hours + 1, 24),
                        'env': team_env,
                    }
                )
                evaluator_rate = compute_rate(Resources(cpu=settings.score_cpu, memory_mb=memory), settings)
        owner = uuid.uuid4().hex
        control.run(f'mkdir -p -m 700 {PILOOP_HOME}')  # a daemon serves its socket only in a home that is its own
        control.write(f'{PILOOP_HOME}/{OWNER_FILE}', owner.encode())
        control.write(CREDENTIALS, credentials(settings))
        control.write(f'{PILOOP_HOME}/config.toml', daytona.home_config(api, n, agents, e, evaluate).encode())
    except Exception:
        control.close()  # none outlives a run that never started
        raise
    usd = daytona.rate(daytona.CONTROL, settings) + n * compute_rate(AGENT, settings) + e * evaluator_rate
    fleet = Fleet(
        control,
        lambda: tagged(settings, {MODAL_OWNER: owner, MODAL_POOL: daytona.AGENTS}),
        usd,
        [control.id],
        control.close,
    )
    fleet.described = {
        'platform': 'team',
        'control': control.id,
        'workers': n,
        'evaluators': e,
        'agents_on': 'modal',
        'evaluators_on': task.scorer,
    }
    return fleet, Piloop(task, arm, settings, hours)


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
"""How a plain arm gets its sandbox, by ``Settings.platform``. A piloop arm runs as a team (``on_team``)."""


def drive(task: Task, arm: PiloopArm | PlainArm, repeat: int, budget: float, hours: float, settings: Settings) -> Path:
    """Run ``arm`` on ``task`` and export it to ``results/<task>/<arm>-<repeat>/``. Its directory."""
    if not settings.gateway or not settings.gateway_token.get_secret_value():
        raise ValueError('set EVALS_GATEWAY and EVALS_GATEWAY_TOKEN: the agents need a model gateway')
    out = settings.results / task.name / f'{arm.name}-{repeat}'
    name = f'{task.name}/{arm.name}-{repeat}'
    env = (exported(settings, name) if isinstance(arm, PiloopArm) else {}) | arm.env
    env['EVALS_EVAL_LOG'] = f'{OUT}/{EVALUATIONS}'
    private = private_files(task, settings)
    with (
        admitted(settings, name, sessions(task, arm, settings, hours)),
        tracer.start_as_current_span('evals.run', attributes={'evals.run': name, 'budget_usd': budget}) as span,
    ):
        out.mkdir(parents=True, exist_ok=False)  # once admitted: a refused run leaves nothing behind
        with tracer.start_as_current_span('evals.sandbox'):
            match arm:
                case PiloopArm():
                    fleet, run = on_team(task, arm, settings, hours, env, name)
                case PlainArm():
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
                run.start(control)
            started_at, started = datetime.now(UTC), time.monotonic()
            stopped = watch(fleet, run, budget, hours, settings, out / 'spend.jsonl', started)
        except Exception as e:
            stopped = f'error: {type(e).__name__}: {e}'[:500]
            raise
        finally:
            span.set_attribute('stopped', stopped)
            kept: list[str] = []
            try:
                with tracer.start_as_current_span('evals.export') as exporting:
                    failed = export(fleet, run, out)
                    exporting.set_attribute('failed', ' | '.join(failed))
                # The control sandbox holds the repo and the run: one that could not give them waits for inspection,
                # until its TTL or its platform's timeout, instead of being deleted with them.
                kept = fleet.ids[:1] if any(f.startswith('control') for f in failed) else []
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
                    'export_failed': failed,
                    'kept': kept,
                    **fleet.described,
                }
                (out / 'run.json').write_text(json.dumps(meta, indent=2))
                archive(out, settings)
            finally:
                if not kept:
                    fleet.close()  # after its export, which it gave
    return out


def export(fleet: Fleet, run: Piloop | Plain, out: Path) -> list[str]:
    """Export the run to ``out/export``, box by box, and return what failed: one box that fails stops no other.

    Each agent's sandbox but the control one gives its session files, under ``workers/<id>``, while it runs; then the
    run ends (a team drains), and the control sandbox gives its repo, its run and its own sessions. ``export.tgz``
    holds all of it, for the archive.
    """
    control, failed = fleet.control, []

    def take(where: str, give: Callable[[], bytes]) -> None:
        try:
            with tarfile.open(fileobj=BytesIO(give())) as tar:
                tar.extractall(out / 'export' / where, filter='data')
        except Exception as e:  # recorded in run.json: the next box still gives its own
            failed.append(f'{where or "control"}: {type(e).__name__}: {e}'[:500])

    def sessions(box: Shell) -> bytes:
        box.run(
            f'mkdir -p {WORK} && tar czf {WORK}/sessions.tgz --ignore-failed-read'
            f' -C {HOME} {" ".join(SESSIONS.values())}',
            timeout=900,
        )
        return box.read(f'{WORK}/sessions.tgz')

    def own() -> bytes:
        try:
            run.export(control)  # a team drains, whatever its sandboxes gave
        finally:
            control.run(
                f'git -C {REPO} bundle create {OUT}/repo.bundle --all'
                f' && tar czf {WORK}/export.tgz --ignore-failed-read -C {WORK} out'
                f' -C {HOME} {" ".join(SESSIONS.values())}',
                timeout=900,
            )
        return control.read(f'{WORK}/export.tgz')

    try:
        agents = fleet.agents()
    except Exception as e:  # recorded: the control sandbox still gives its own
        agents = {}
        failed.append(f'workers: {type(e).__name__}: {e}'[:500])
    for name, box in agents.items():
        if box is not control:
            take(f'workers/{name}', lambda box=box: sessions(box))
    take('', own)
    if (out / 'export').is_dir():
        with tarfile.open(out / 'export.tgz', 'w:gz') as tar:
            for given in sorted((out / 'export').iterdir()):
                tar.add(given, arcname=given.name)
    return failed


def probed[T](probe: Callable[[], T], unknown: T, span: trace.Span) -> T:
    """``probe()``, or ``unknown`` when it fails or stalls (a Modal call that hangs), recorded on ``span``.

    A status probe is asked again at the next sample: only a spend sample that fails ``sample_attempts`` times stops a
    run.
    """
    try:
        return probe()
    except (ConnectionError, RuntimeError, TimeoutError) as e:
        span.add_event('evals.probe_failed', {'error': str(e)[:300]})
        return unknown


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
    seen: dict[str, Counts] = {}
    with log.open('w') as f:

        def record() -> Sample | RuntimeError | ConnectionError | TimeoutError:
            """The spend now, written to ``log``; or the error of the last attempt, when every attempt failed."""
            with tracer.start_as_current_span('evals.spend') as span:
                failed: RuntimeError | ConnectionError | TimeoutError = RuntimeError('no attempt: sample_attempts is 0')
                for attempt in range(settings.sample_attempts):
                    try:
                        now = sample(fleet.agents(), seen, fleet.usd_per_hour, (time.monotonic() - started) / 3600)
                    # Modal's link to the sandbox drops now and then, and the meter can be killed (exit 137 when the
                    # sandbox runs out of memory): try again.
                    except (ConnectionError, RuntimeError, TimeoutError) as e:
                        span.add_event('evals.sample_retry', {'attempt': attempt, 'error': str(e)})
                        failed = e
                        if probed(fleet.control.ended, False, span):
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
                with tracer.start_as_current_span('evals.ended') as span:
                    stopped = probed(lambda: run.ended(fleet.control), None, span) or ''
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
