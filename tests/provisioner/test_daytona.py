"""The ``daytona`` provisioner, against a stand-in for Daytona's client: no network, no credentials, and the conformance
check of every provisioner."""

import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from itertools import count

import daytona
import pytest
from daytona import (
    CreateSandboxFromImageParams,
    CreateSandboxFromSnapshotParams,
    ListSandboxesQuery,
    Resources,
    SandboxState,
    SessionExecuteRequest,
)
from pydantic import SecretStr

from piloop.otel import Tracer
from piloop.plugins.host import Check
from piloop.plugins.provisioner.contract import PROVISIONERS, InstanceState, Launch, ProvisionerHost, StartRefused
from piloop.plugins.provisioner.daytona import Daytona, DaytonaProvisioning
from piloop.plugins.provisioner.kit.conformance import check_provisioning

IDS = count(1)
LAUNCH = Launch(
    name='eval-0123abcd',
    pool='eval',
    argv=['piloop', 'worker', '--server', 'https://piloop.test', '--pool', 'eval'],
    env={'PILOOP_HTTPS_TOKEN': SecretStr("plt_se'cret")},
)

type Params = CreateSandboxFromSnapshotParams | CreateSandboxFromImageParams
type Sandboxes = list[FakeSandbox]


@dataclass
class FakeSandbox:
    """A sandbox, and its toolbox: ``fs`` and ``process`` are the sandbox itself."""

    params: Params
    id: str = field(default_factory=lambda: f'sb-{next(IDS)}')
    state: SandboxState = SandboxState.STARTED
    files: dict[str, bytes] = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)
    commands: list[tuple[str, SessionExecuteRequest]] = field(default_factory=list)
    fails: Exception | None = None
    gone: bool = False
    """Destroyed since it was listed."""
    busy: bool = False
    """Its state changes: Daytona refuses a delete."""
    stale: SandboxState | None = None
    """What the next list shows in place of its state: Daytona's list lags its deletes by seconds."""

    @property
    def labels(self) -> dict[str, str]:
        assert self.params.labels is not None
        return {**self.params.labels, 'code-toolbox-language': 'python'}  # Daytona adds its own

    @property
    def fs(self) -> FakeSandbox:
        return self

    @property
    def process(self) -> FakeSandbox:
        return self

    def upload_file(self, src: bytes, dst: str) -> None:
        if self.fails is not None:
            raise self.fails
        self.files[dst] = src

    def create_session(self, session_id: str) -> None:
        self.sessions.append(session_id)

    def execute_session_command(self, session_id: str, req: SessionExecuteRequest) -> None:
        self.commands.append((session_id, req))


@dataclass
class FakeDaytona:
    """Daytona's client and what it holds: the sandboxes, and what the next create raises."""

    sandboxes: Sandboxes = field(default_factory=list)
    clients: int = 0
    raises: Exception | None = None
    fails: Exception | None = None
    """What the toolbox of the next sandbox raises."""

    def __call__(self) -> FakeDaytona:
        self.clients += 1
        return self

    def create(self, params: Params, *, timeout: float) -> FakeSandbox:
        if self.raises is not None:
            raise self.raises
        assert timeout == 600
        sandbox = FakeSandbox(params, fails=self.fails)
        self.sandboxes.append(sandbox)
        return sandbox

    def list(self, query: ListSandboxesQuery) -> Iterator[FakeSandbox]:
        assert query.labels is not None
        for sandbox in self.sandboxes:
            if sandbox.state is SandboxState.DESTROYED or not query.labels.items() <= sandbox.labels.items():
                continue
            shown, sandbox.stale = sandbox.stale or sandbox.state, None
            if shown is SandboxState.DESTROYING:
                sandbox.state = SandboxState.DESTROYED  # gone by the next list
            yield replace(sandbox, state=shown)

    def get(self, sandbox_id: str) -> FakeSandbox:
        [sandbox] = [s for s in self.sandboxes if s.id == sandbox_id]
        if sandbox.gone:
            raise daytona.DaytonaNotFoundError('no such sandbox')
        return sandbox

    def delete(self, listed: FakeSandbox) -> None:
        sandbox = self.get(listed.id) if not listed.busy else listed
        if sandbox.busy or sandbox.state is SandboxState.DESTROYING:
            raise daytona.DaytonaConflictError('Failed to remove sandbox: Sandbox state change in progress')
        sandbox.state, sandbox.stale = SandboxState.DESTROYING, sandbox.state


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> FakeDaytona:
    fake = FakeDaytona()
    monkeypatch.setattr(daytona, 'Daytona', fake)
    return fake


def opened(owner: str, **keys: object) -> DaytonaProvisioning:
    config = PROVISIONERS.validated({'provider': 'daytona', 'snapshot': 'piloop-worker-0.5', **keys})
    assert isinstance(config, Daytona)
    return config.open(ProvisionerHost(owner, Tracer(None)))


def test_daytona_passes_the_provisioner_conformance_check(sdk: FakeDaytona) -> None:
    check_provisioning(opened, LAUNCH, timeout=1)


def test_a_sandbox_runs_the_argv_with_the_credential_in_a_file_and_this_servers_labels(sdk: FakeDaytona) -> None:
    keys = {'env': {'PILOOP_TICK_SECONDS': '2'}, 'secrets': {'HF_TOKEN': 'hf-read'}, 'ttl_hours': 2}
    provisioning = opened('owner-a', **keys)
    assert sdk.clients == 0, 'opening touches no network'
    provisioning.start(LAUNCH)
    [sandbox] = sdk.sandboxes
    params = sandbox.params
    assert isinstance(params, CreateSandboxFromSnapshotParams) and params.snapshot == 'piloop-worker-0.5'
    assert params.labels == {'piloop-owner': 'owner-a', 'piloop-name': LAUNCH.name, 'piloop-pool': 'eval'}
    assert (params.env_vars, params.secrets) == ({'PILOOP_TICK_SECONDS': '2'}, {'HF_TOKEN': 'hf-read'})
    assert (params.ttl_minutes, params.auto_stop_interval, params.ephemeral, params.public) == (120, 0, True, False)
    [(path, exports)] = sandbox.files.items()
    assert exports == b"export PILOOP_HTTPS_TOKEN='plt_se'\"'\"'cret'\n"
    [(session, request)] = sandbox.commands
    assert sandbox.sessions == [session] and request.run_async
    assert request.command == f'. {path} && rm {path} && piloop worker --server https://piloop.test --pool eval'
    assert 'plt_se' not in str(params), "the credential is not in the sandbox's environment, which Daytona's API shows"
    assert sdk.clients == 1
    [instance] = provisioning.list()
    assert (instance.name, instance.pool, instance.state, instance.handle) == (
        LAUNCH.name,
        'eval',
        InstanceState.RUNNING,
        {'id': sandbox.id},
    )


def test_an_image_takes_the_resources_of_each_sandbox(sdk: FakeDaytona) -> None:
    keys = {'snapshot': None, 'image': 'ghcr.io/acme/piloop-worker:0.5', 'cpu': 4, 'memory_gib': 8, 'disk_gib': 20}
    opened('owner-a', **keys).start(LAUNCH)
    [sandbox] = sdk.sandboxes
    assert isinstance(sandbox.params, CreateSandboxFromImageParams)
    assert (sandbox.params.image, sandbox.params.resources) == (
        'ghcr.io/acme/piloop-worker:0.5',
        Resources(cpu=4, memory=8, disk=20),
    )


@pytest.mark.parametrize(
    ('keys', 'error'),
    [
        ({}, 'a snapshot or an image'),
        ({'snapshot': 's', 'image': 'i'}, 'a snapshot or an image'),
        ({'snapshot': 's', 'cpu': 2}, 'the snapshot fixes'),
        ({'image': 'i', 'cpu': 5}, 'cpu'),
        ({'image': 'i', 'ttl_hours': 0}, 'ttl_hours'),
    ],
)
def test_the_daytona_table_names_one_source_and_sizes_only_an_image(keys: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        PROVISIONERS.validated({'provider': 'daytona', **keys})


@pytest.mark.parametrize(
    'error',
    [
        daytona.DaytonaBadRequestError('Snapshot piloop-worker-0.5 not found'),
        daytona.DaytonaForbiddenError('Total CPU limit exceeded'),
        daytona.DaytonaAuthenticationError('bad key'),
    ],
)
def test_what_daytona_refuses_is_a_refused_start_and_anything_else_is_tried_again(
    sdk: FakeDaytona, error: Exception
) -> None:
    sdk.raises = error
    with pytest.raises(StartRefused, match=type(error).__name__):
        opened('owner-a').start(LAUNCH)
    sdk.raises = daytona.DaytonaRateLimitError('slow down')
    with pytest.raises(daytona.DaytonaRateLimitError):
        opened('owner-a').start(LAUNCH)


def test_a_sandbox_whose_worker_did_not_start_is_deleted_so_that_the_next_start_makes_another(
    sdk: FakeDaytona,
) -> None:
    provisioning = opened('owner-a')
    sdk.fails = daytona.DaytonaConnectionError('reset')
    with pytest.raises(daytona.DaytonaConnectionError):
        provisioning.start(LAUNCH)
    assert [i.state for i in provisioning.list()] == [InstanceState.RUNNING], 'listed as it was, for a while'
    sdk.fails = None
    provisioning.start(LAUNCH)
    assert [s.state for s in sdk.sandboxes] == [SandboxState.DESTROYED, SandboxState.STARTED]


@pytest.mark.parametrize(
    ('state', 'listed'),
    [
        (SandboxState.PENDING_BUILD, InstanceState.PENDING),
        (SandboxState.STARTING, InstanceState.PENDING),
        (SandboxState.ERROR, InstanceState.ENDED),
        (SandboxState.STOPPED, InstanceState.ENDED),
        (SandboxState.DESTROYING, InstanceState.ENDED),
    ],
)
def test_a_sandbox_is_listed_by_its_state_until_daytona_drops_it(
    sdk: FakeDaytona, state: SandboxState, listed: InstanceState
) -> None:
    provisioning = opened('owner-a')
    provisioning.start(LAUNCH)
    sdk.sandboxes[0].state = state
    assert [i.state for i in provisioning.list()] == [listed]
    sdk.sandboxes[0].state = SandboxState.DESTROYED
    assert provisioning.list() == []


def test_a_sandbox_that_daytona_deleted_since_it_listed_it_is_stopped(sdk: FakeDaytona) -> None:
    provisioning = opened('owner-a')
    provisioning.start(LAUNCH)
    sdk.sandboxes[0].gone = True
    provisioning.stop(LAUNCH.name)


@pytest.mark.parametrize('gone', [False, True])
def test_a_sandbox_whose_state_changes_is_stopped_again_by_the_next_pass_unless_it_is_gone(
    sdk: FakeDaytona, gone: bool
) -> None:
    provisioning = opened('owner-a')
    provisioning.start(LAUNCH)
    sdk.sandboxes[0].busy, sdk.sandboxes[0].gone = True, gone
    if gone:
        provisioning.stop(LAUNCH.name)
    else:
        with pytest.raises(daytona.DaytonaConflictError):
            provisioning.stop(LAUNCH.name)


def test_daytona_without_its_extra_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, 'daytona', None)
    with pytest.raises(ImportError, match=r'piloop\[daytona\]'):
        Daytona(snapshot='s').open(ProvisionerHost('owner-a', Tracer(None)))


def test_the_doctor_finds_daytona_s_key_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('DAYTONA_API_KEY', 'dtn_1')
    provider = Daytona(snapshot='s')
    assert provider.checks() == [
        Check('daytona', 'daytona is installed'),
        Check('daytona login', 'DAYTONA_API_KEY is set'),
    ]
    monkeypatch.delenv('DAYTONA_API_KEY')
    assert provider.checks()[1] == Check(
        'daytona login', 'DAYTONA_API_KEY is not set', 'set DAYTONA_API_KEY where the daemon runs'
    )


@pytest.mark.parametrize('verb', ['list', 'start', 'stop'])
def test_a_daytona_call_that_hangs_fails_in_its_seconds(
    sdk: FakeDaytona, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    provisioning = opened('owner-a', call_seconds=0.05, start_seconds=0.05)
    released = threading.Event()

    def hung(query: ListSandboxesQuery) -> Iterator[FakeSandbox]:
        released.wait()
        return iter(())

    monkeypatch.setattr(sdk, 'list', hung)
    verbs = {
        'list': provisioning.list,
        'start': lambda: provisioning.start(LAUNCH),
        'stop': lambda: provisioning.stop('x'),
    }
    with pytest.raises(TimeoutError, match=r'did not answer in 0\.05 s'):
        verbs[verb]()
    released.set()
