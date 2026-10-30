"""The types of the eval runner: a task, an arm, and the result of one run of an arm on a task."""

import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).parent
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
    data: list[str] = []
    """Shell lines that write the dev data into the current directory when the image is built. It is copied into the
    repo, and git ignores it."""
    piloop: dict[str, JsonValue]
    """The task's own keys of ``piloop.toml``: its name, evaluator and metrics. An arm adds the rest."""

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


class PiloopArm(Frozen):
    """A piloop campaign: one piloop install, its agents, and the keys of ``piloop.toml`` it adds to the task's."""

    kind: Literal['piloop']
    name: str
    piloop: str
    """What ``uv tool install`` installs, such as ``piloop[modal]==0.5.0`` or a git URL at a commit."""
    agents: list[Agent] = Field(min_length=1)
    """The rotation of ``[plugins.harness] use``, each with its model."""
    overlay: dict[str, JsonValue] = {}
    """More keys of ``piloop.toml``, merged over the task's and the agents'."""
    resources: Resources


class PlainArm(Frozen):
    """One agent session with no piloop: it owns the change and the dev evaluator."""

    kind: Literal['plain']
    name: str
    agent: Agent
    effort: str = 'high'
    """How hard it thinks, as the harness names it: piloop's default for its roles."""
    resources: Resources


Arm = Annotated[PiloopArm | PlainArm, Field(discriminator='kind')]


class ArmFile(BaseModel):
    """Picks the kind of arm that a file describes."""

    arm: Arm


def load_arm(name: str) -> PiloopArm | PlainArm:
    """The arm of ``arms/<name>.toml``, or of the file ``name`` when it ends with ``.toml``."""
    path = Path(name) if name.endswith('.toml') else ARMS / f'{name}.toml'
    return ArmFile.model_validate({'arm': tomllib.loads(path.read_text())}).arm


class Sample(Frozen):
    """What a run had spent at one moment, list price, in USD."""

    hours: float
    models_usd: float
    compute_usd: float

    @property
    def usd(self) -> float:
        """Models and compute."""
        return self.models_usd + self.compute_usd


class Point(Frozen):
    """A new head of a run: the version it would hand in from this moment on."""

    revision: str
    hours: float
    usd: float
    dev: float | None
    """Its normalised dev score, re-measured outside the run with the task's own evaluator. None: it failed."""
    sealed: float | None
    """Its normalised sealed score. None: it failed."""
    sealed_detail: str = ''


class Funnel(Frozen):
    """Ideas → runs → submissions → kept. A plain session has no ideas; its runs are its sessions."""

    ideas: int | None
    runs: int
    submissions: int
    kept: int


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
    cpu_usd_per_core_hour: float = 0.1419
    """The list price of a sandbox's CPU core."""
    memory_usd_per_gib_hour: float = 0.0242
    poll_seconds: float = 60
    """How often the driver reads a run's spend."""
    stop_grace_minutes: float = 15
    """How long a stopped campaign gets to score what its agents committed."""
    score_timeout_minutes: float = 30
    score_cpu: float = 2
    """The CPU cores of each scoring sandbox: the dev and sealed evaluators run one after the other."""
    score_memory_mb: int = 4096
    codex_thread_seconds: float = 120
    otlp_endpoint: str | None = Field(default=None, validation_alias='OTEL_EXPORTER_OTLP_ENDPOINT')
    """The OTLP/HTTP collector of the driver's spans and the piloop arms' daemons. Unset: spans go to the console."""
    otlp_headers: SecretStr | None = Field(default=None, validation_alias='OTEL_EXPORTER_OTLP_HEADERS')
    """The headers that the collector wants, such as ``authorization=Bearer <token>``."""
    otel_resource_attributes: str = Field(default='', validation_alias='OTEL_RESOURCE_ATTRIBUTES')
    """How long a plain Codex session gets to print its thread id."""
    score_concurrency: int = 8
