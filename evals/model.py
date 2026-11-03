"""The types of the eval runner: a task, an arm, and the result of one run of an arm on a task."""

import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).parent
DAYTONA_MEMORY_MB = 8192
"""The most memory of a Daytona sandbox."""
TASKS = ROOT / 'tasks'
ARMS = ROOT / 'arms'


class Frozen(BaseModel):
    """A model of a TOML file or a result: no unknown keys, never changed."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)


class Task(Frozen):
    """``tasks/<name>/task.toml``: a starting repo (``repo/``), its dev evaluator and its sealed scorer (``sealed.py``)."""

    name: str
    metric: str
    """The metric of the dev evaluator that the task improves."""
    direction: Literal['lower', 'higher']
    dev: list[str]
    """The dev evaluator: the command that agents run in the repo, which prints ``{"metrics": {...}}``."""
    dev_base: float
    """The dev metric of the starting repo: normalised score 0."""
    dev_ref: float
    """A strong known value of the dev metric: normalised score 1."""
    setup: list[str] = []
    """Shell lines that install the task's tools into the image as root, before its data: a compiler, a toolchain,
    the packages its scorer imports."""
    gpu: str | None = None
    """The Modal GPU type of every sandbox that runs the task, such as ``L4``. None: CPU only."""
    cloud: str | None = None
    """The Modal cloud of every sandbox that runs the task, which pins its CPU type for a task scored on timing:
    ``gcp`` gave AMD Zen 4 (family 25, model 17) in 26 of 26 sandboxes, ``aws`` Intel Ice Lake. None: any."""
    score_repeats: int = Field(default=1, ge=1)
    """How many sandboxes score each version: its scores are their means. More than 1 for a scorer that times."""
    data: list[str] = []
    """Shell lines that write the dev data into the current directory when the image is built. It is copied into the
    repo, and git ignores it."""
    network: Literal['open', 'gateway'] = 'open'
    """What an agent's sandbox reaches. ``gateway``: the model gateway's host alone, for a task whose answers are on
    the web, so that a run measures the agents and not a search. ``setup`` and ``data`` run when the image is built,
    with network either way; scoring sandboxes reach nothing."""
    memory_limit_mb: int | None = Field(default=None, gt=0)
    """The memory every sandbox of the task reserves and is capped at, or its arm's own request when that is larger.
    Modal's memory alone is a request, which a sandbox may outgrow until the host kills it. None: a request only."""
    scorer: Literal['modal', 'daytona']
    """Where its versions are scored afterwards: ``daytona``, in a sandbox of ``daytona.EVALUATOR`` on the same
    platform as the evaluators that decide its verdicts (``EVALS_PLATFORM=daytona``); ``modal``, for a task that needs a
    GPU or more memory than a Daytona sandbox has."""
    solve: float | None = Field(default=None, gt=0)
    """The sealed score that solves the task, such as 1 for every target proved. A task with it is solvable, and its
    report leads with the time, $ and tokens to solve, and the score at each hour; the curve keeps partial credit.
    None: a task scored on how far a run gets."""
    private: bool = False
    """The task has a private split: the files of ``<sealed_uri>/<task>.private/``, which the driver copies into the
    repo before its starting commit. They are in no repository and no image, and no sandbox can read sealed storage."""
    piloop: dict[str, JsonValue]
    """The task's own keys of ``piloop.toml``: its name, evaluator and metrics. An arm adds the rest."""

    @model_validator(mode='after')
    def _scorer_fits(self) -> Task:
        """A ``daytona`` scorer has no GPU and at most ``DAYTONA_MEMORY_MB``."""
        if self.scorer == 'daytona' and (self.gpu or (self.memory_limit_mb or 0) > DAYTONA_MEMORY_MB):
            raise ValueError(
                f'{self.name}: scorer = "daytona" has no GPU and at most {DAYTONA_MEMORY_MB} MiB; it needs '
                f'gpu = {self.gpu!r}, memory_limit_mb = {self.memory_limit_mb}: use scorer = "modal"'
            )
        return self

    @classmethod
    def load(cls, name: str) -> Task:
        """The task of ``tasks/<name>``."""
        return cls.model_validate(tomllib.loads((TASKS / name / 'task.toml').read_text()))

    @property
    def repo(self) -> Path:
        """The files of the starting commit."""
        return TASKS / self.name / 'repo'

    def normalised(self, dev: float) -> float:
        """A dev metric as a score: 0 at the starting repo, 1 at ``dev_ref``."""
        return (dev - self.dev_base) / (self.dev_ref - self.dev_base)


class Agent(Frozen):
    """One coding agent's harness and model."""

    harness: Literal['codex', 'claude']
    model: str


class Resources(Frozen):
    """The sandbox of a run."""

    cpu: float = Field(gt=0)
    memory_mb: int = Field(gt=0)
    gpus: int = Field(default=1, ge=1)
    """How many of the task's GPUs the sandbox gets, when the task has a GPU type. ``gpu-lock`` gives each evaluation
    one of them to itself."""


class PiloopArm(Frozen):
    """A piloop campaign: one piloop install, its agents, and the keys of ``piloop.toml`` it adds to the task's."""

    kind: Literal['piloop']
    name: str
    piloop: str
    """What ``uv tool install`` installs, such as ``piloop[modal,daytona]==0.6.0`` or a git URL at a commit: a piloop
    with the ``modal`` and ``daytona`` provisioners and their extras, which start the arm's team."""
    agents: list[Agent] = Field(min_length=1)
    """The rotation of ``[plugins.harness] use``, each with its model."""
    overlay: dict[str, JsonValue] = {}
    """More keys of ``piloop.toml``, merged over the task's and the agents'."""
    split: Literal['visible', 'sealed'] = 'visible'
    """``visible``: the daemon scores with the task's evaluator alone. ``sealed``: the campaign also gets the task's dev
    command as ``[evaluator] dev``, and a sealed split of its own as ``[evaluator] sealed``, kept in its home."""
    instruction: str = ''
    """A paragraph added to what each worker reads: a piloop arm's implementer brief, a plain session's prompt."""
    charters: dict[str, dict[str, str]] = {}
    """For each task, for each role that does runs: what its workers own, appended to that role's brief. An arm with
    charters runs only on the tasks that it names."""
    env: dict[str, str] = {}
    """Variables of the arm's sandboxes, which a task's evaluator may read: ``EVALS_DEV_LOOP = "slow"`` turns a warm
    dev loop off."""


class PlainArm(Frozen):
    """One agent session with no piloop: it owns the change and the dev evaluator."""

    kind: Literal['plain']
    name: str
    agent: Agent
    effort: str = 'high'
    """How hard it thinks, as the harness names it: piloop's default for its roles."""
    instruction: str = ''
    """A paragraph added to what each worker reads: a piloop arm's implementer brief, a plain session's prompt."""
    env: dict[str, str] = {}
    """Variables of the arm's sandbox, which a task's evaluator may read: ``EVALS_DEV_LOOP = "slow"`` turns a warm
    dev loop off."""
    resources: Resources


Arm = Annotated[PiloopArm | PlainArm, Field(discriminator='kind')]


class ArmFile(BaseModel):
    """Picks the kind of arm that a file describes."""

    arm: Arm


def load_arm(name: str) -> PiloopArm | PlainArm:
    """The arm of ``arms/<name>.toml``, or of the file ``name`` when it ends with ``.toml``."""
    path = Path(name) if name.endswith('.toml') else ARMS / f'{name}.toml'
    return ArmFile.model_validate({'arm': tomllib.loads(path.read_text())}).arm


class Tokens(Frozen):
    """The tokens of a run's agents, summed over every harness and model, as ``meter.py`` counts them."""

    input: int = 0
    """Input tokens not served by the cache."""
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    turns: int = 0

    @property
    def total(self) -> int:
        """Every token of every kind: input, output, cache reads and cache writes."""
        return self.input + self.output + self.cache_read + self.cache_write


class Sample(Frozen):
    """What a run had spent at one moment, list price, in USD."""

    hours: float
    models_usd: float
    compute_usd: float
    tokens: Tokens | None = None
    """What the models' spend bought. None: a sample from before the driver kept the tokens."""

    @property
    def usd(self) -> float:
        """Models and compute."""
        return self.models_usd + self.compute_usd


class Point(Frozen):
    """A new head of a run: the version it would hand in from this moment on."""

    revision: str
    hours: float
    usd: float
    models_usd: float | None = None
    """The models' share of ``usd``. None: a result from before the driver kept it."""
    tokens: Tokens | None = None
    """The tokens spent by then. None: a result from before the driver kept them."""
    dev: float | None
    """Its normalised dev score, re-measured outside the run with the task's own evaluator. None: it failed."""
    sealed: float | None
    """Its normalised sealed score. None: it failed."""
    sealed_detail: str = ''


class Funnel(Frozen):
    """Ideas → runs → submissions → kept. A plain session has no ideas; its runs are its sessions.

    ``kept`` is a verdict run's keeps, a pool run's new bests, and a plain session's commits on ``main``.
    """

    ideas: int | None
    runs: int
    submissions: int
    kept: int


class PoolCounts(Frozen):
    """What a piloop run of the pool model (0.6) did beyond its funnel."""

    confirmations: int
    """Candidates evaluated more than once under the campaign's evaluator: confirmed, as their first score beat the
    best's. The baseline is not one."""
    sealed_scorings: int
    """Evaluations on the campaign's sealed split, the baseline's included."""
    based_runs: int
    """Runs whose claim chose another candidate than the best they started from (``--base``)."""


class Loop(Frozen):
    """A run's feedback loop."""

    dev_evals: int
    dev_p50_s: float | None
    dev_p90_s: float | None
    dev_per_hour: float
    submissions_per_hour: float
    usd_per_experiment: float | None
    """Spend over dev evaluations plus submissions."""
    verdict_p50_s: float | None
    """Submitted to judged. None: a plain session, which judges itself."""
    visible_p50_s: float | None
    """Submitted to measured on the visible split: the queue plus the evaluation."""
    sealed_p50_s: float | None
    """Measured on the visible split to measured on the sealed split."""
    drift: float | None
    """The median dev evaluation of the last third of the run over that of the first third: above 1, the loop slowed."""
    protocol_calls: int | None = None
    """Shell commands that speak piloop's protocol (claim, propose, submit, run end, notes). None: not read."""
    protocol_seconds: float | None = None
    """Their wall time, waits for verdicts included."""
    protocol_token_share: float | None = None
    """The share of the session's tokens in the responses that asked for them."""


class SelfTest(Frozen):
    """The task's dev evaluator on the starting repo, run in the run's sandbox before the agents start."""

    cpu: str
    """The vendor, family and model of the sandbox's CPU, and its AVX-512: ``AuthenticAMD 25/17 avx512``."""
    values: list[float]
    """The dev metric of each run."""


REMOVED = ('built_on', 'replaced')
"""Fields of results from earlier rounds that ``keeps``, ``keeps_in_final`` and ``races`` replaced: dropped on load."""


class Result(Frozen):
    """One run of an arm on a task: ``results/<task>/<arm>-<repeat>.json``."""

    task: str
    arm: str
    repeat: int
    piloop: str | None
    """The piloop that ran, as ``piloop --version`` says. None: a plain arm."""
    started_at: datetime
    budget_usd: float
    max_hours: float
    stopped: str
    """Why the run ended: ``budget``, ``hours``, or the campaign's own stop."""
    spend: list[Sample]
    curve: list[Point]
    """The base and then each new head, in order."""
    funnel: Funnel
    escalations: int
    interventions: int
    lead_token_share: float | None
    """The orchestrator's share of the tokens. None: a plain arm."""
    restarts: int | None = None
    """How many times the daemon's supervisor started it again after it died. None: a plain arm, or a run whose daemon
    had no supervisor."""
    messages_sent: int | None = None
    """Messages that piloop's daemon wrote to running workers, such as a moved head. None: a plain arm."""
    messages_delivered: int | None = None
    messages_refused: int | None = None
    """Messages that a worker's harness could not take mid-turn: refused, never queued."""
    loop: Loop | None = None
    """The run's feedback loop. None: a result written before it was measured."""
    selftest: SelfTest | None = None
    """How much the dev evaluator varies on the run's machine. None: a run from before the self-test."""

    @model_validator(mode='before')
    @classmethod
    def removed(cls, data: object) -> object:
        """A result from an earlier round loads without its ``REMOVED`` fields; any other unknown key still fails."""
        return {k: v for k, v in data.items() if k not in REMOVED} if isinstance(data, dict) else data

    keeps: dict[str, int] = {}
    """The keeps of each role that does runs (``evals.team``)."""
    keeps_in_final: dict[str, int] = {}
    """The keeps of each role that the final head contains: the gains that stacked. Empty for a pool run, which has
    ``on_final_line`` instead."""
    on_final_line: dict[str, int] = {}
    """A pool run's candidates on the final best's line (its chain of parents, the best included), by the role of
    their run."""
    pool: PoolCounts | None = None
    """A pool run's confirmations, sealed scorings and runs from another base. None: a plain run, or a verdict run."""
    races: int | None = None
    """Submissions that beat the version they started from, but not the head that another run's keep had moved on to
    by the time they were judged. None: a plain run, or a pool run, which has no head to race."""
    notes_written: dict[str, int] = {}
    """The notes that the workers of each role wrote."""
    notes_read: dict[str, int] = {}
    """The notes that a worker of another run read, by ``author role>reader role``."""

    @property
    def first_keep(self) -> Point | None:
        """The first head after the base."""
        return self.curve[1] if len(self.curve) > 1 else None

    @property
    def final(self) -> Point:
        """The head at the end, within the budget."""
        return self.curve[-1]

    @property
    def gap(self) -> float | None:
        """Dev minus sealed score of the final head: how much of the visible gain did not carry over."""
        final = self.final
        return None if final.dev is None or final.sealed is None else final.dev - final.sealed


class Settings(BaseSettings):
    """The runner's settings, from ``EVALS_<NAME>`` variables. None of them names a private place in this repo."""

    model_config = SettingsConfigDict(env_prefix='EVALS_', frozen=True)

    gateway: str = ''
    """The model gateway that agents call: an Anthropic- and OpenAI-compatible base URL."""
    gateway_token: SecretStr = SecretStr('')
    """The gateway's bearer token. Agents get it in their environment."""
    sealed_uri: str = ''
    """``s3://bucket/prefix`` of the sealed data: ``<prefix>/<task>/`` holds the files that ``sealed.py`` reads."""
    archive_uri: str = ''
    """``s3://bucket/prefix`` where each run's raw export goes. Unset: kept only under ``results``."""
    results: Path = ROOT / 'results'
    modal_app: str = 'piloop-evals'
    modal_environment: str | None = None
    platform: Literal['modal', 'daytona'] = 'modal'
    """Where a plain arm's session runs: ``modal``, a Modal sandbox of the arm's ``resources``; ``daytona``, one of
    ``drive.AGENT``, as each agent of a piloop arm. A piloop arm always runs as a team (``drive.on_team``)."""
    daytona_api_key: SecretStr = SecretStr('')
    """Daytona's API key: a piloop arm's control sandbox and evaluators, and the scorings of a task scored there."""
    daytona_usd_per_vcpu_hour: float = 0.0504
    """Daytona's list price of a vCPU."""
    daytona_usd_per_gib_hour: float = 0.0162
    """Daytona's list price of a GiB of memory."""
    max_sessions: int = Field(default=20, ge=1)
    """The most agent sessions that this machine's drivers keep open at once on the model gateway's pool: a run that
    would take more is refused (``drive.admitted``)."""
    session_ledger: Path = Path('/tmp/evals-sessions')
    """The entries of the sessions that the drivers hold, one file each, under a lock."""
    evaluators_per_worker: float = 1.0
    """The evaluator sandboxes of a piloop arm for each of its workers: each runs one evaluation at a time."""
    cpu_usd_per_core_hour: float = 0.1419
    """The list price of a sandbox's CPU core."""
    memory_usd_per_gib_hour: float = 0.0242
    report_css: str = ''
    """A stylesheet that the report's pages link. Unset: the browser's own."""
    report_plot_js: list[str] = ['https://cdn.plot.ly/plotly-2.35.2.min.js']
    """The scripts that the report's pages load to draw charts."""
    report_plot_fn: str = 'Plotly.newPlot'
    """The function that draws a chart, called as ``Plotly.newPlot`` is: element id, traces, layout."""
    poll_seconds: float = 60
    sample_attempts: int = 5
    """How many times the driver tries to read a run's spend before it gives the run up: Modal's link to a sandbox
    drops now and then."""
    """How often the driver reads a run's spend."""
    stop_grace_minutes: float = 15
    selftest_runs: int = 5
    """How many times a run evaluates its starting repo before the agents start (``SelfTest``)."""
    """How long a stopped campaign gets to score what its agents committed."""
    score_timeout_minutes: float = 30
    score_cpu: float = 2
    """The CPU cores of each scoring sandbox: the dev and sealed evaluators run one after the other."""
    score_memory_mb: int = 4096
    codex_reasoning_summary: str = 'detailed'
    """How much of its reasoning each Codex agent summarises into its session file (``model_reasoning_summary``)."""
    evaluation_nice: int = 10
    """The ``nice`` of a piloop arm's evaluations, its own and its workers' dev runs: the daemon keeps CPU for its
    database."""
    daemon_start_seconds: float = 60
    """How long a piloop daemon gets to answer after its supervisor starts it."""
    otlp_endpoint: str | None = Field(default=None, validation_alias='OTEL_EXPORTER_OTLP_ENDPOINT')
    """The OTLP/HTTP collector of the driver's spans and the piloop arms' daemons. Unset: spans go to the console."""
    otlp_headers: SecretStr | None = Field(default=None, validation_alias='OTEL_EXPORTER_OTLP_HEADERS')
    """The headers that the collector wants, such as ``authorization=Bearer <token>``."""
    otel_resource_attributes: str = Field(default='', validation_alias='OTEL_RESOURCE_ATTRIBUTES')
    """How long a plain Codex session gets to print its thread id."""
    score_concurrency: int = 8
