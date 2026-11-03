"""``daytona``: each worker of the pool is a Daytona sandbox that runs ``piloop worker`` from the snapshot that you name.

```toml
[plugins.executor.pools.eval.provisioner.compute]
provider = "daytona"
snapshot = "piloop-worker-0.5"
env = { PILOOP_PLUGINS = '{"cas": {"provider": "http", "url": "https://piloop-cas.example.com"}}' }
```

The snapshot, or the ``image`` that Daytona builds one from, has ``piloop`` on its ``PATH``, with the evaluator runners
and harnesses that the pool's jobs need. A snapshot fixes the size of its sandboxes; an image takes ``cpu``,
``memory_gib`` and ``disk_gib``. A sandbox has 4 vCPUs at most, as a CPU quota, and its memory is a hard limit.

Once the sandbox runs, the provider writes the worker's credential to a file in it, and starts ``piloop worker`` in a
session whose command reads the file and deletes it. Daytona's API shows a sandbox's environment and the commands of
its sessions, so the credential is in neither. Its labels name its worker, its pool and this server's owner, so
``list`` finds this server's sandboxes alone.

``secrets`` mounts Daytona Secrets as variables: the sandbox sees a placeholder, which Daytona replaces with the value in
the requests to the Secret's hosts. They suit a bearer token of the worker itself, never a key that signs, and never a
job's secrets (piloop's ``secrets`` capability gives those).

``stop`` deletes the sandbox. Daytona deletes one after ``ttl_hours`` too, whatever it runs: its lease then expires,
and the pool starts another one if a job still waits. A sandbox does not stop when its worker exits: the server stops
it once its worker has drained, or went silent.

The server needs ``DAYTONA_API_KEY``, and the ``daytona`` extra (``uv tool install 'piloop[daytona]'``), imported when
the pool's provisioner opens.
"""

import math
import os
import shlex
from collections.abc import Sequence
from typing import TYPE_CHECKING, Self

from pydantic import Field, SecretStr, model_validator

from piloop.plugins.host import Check, extra
from piloop.plugins.provisioner.contract import (
    Instance,
    InstanceState,
    Launch,
    ProvisionerConfig,
    ProvisionerHost,
    StartRefused,
)

if TYPE_CHECKING:
    import daytona as daytona_sdk

OWNER, NAME, POOL = 'piloop-owner', 'piloop-name', 'piloop-pool'
"""The labels of a sandbox."""
KEY = 'DAYTONA_API_KEY'
SESSION = 'piloop-worker'
CREDENTIAL = '/tmp/piloop-worker.env'
"""Where the worker's credential waits in the sandbox until its command reads it."""
START_SECONDS = 600
"""How long a start waits for its sandbox to run: an image's first build takes minutes."""


class Daytona(ProvisionerConfig):
    """``provider = "daytona"``: a Daytona sandbox for each worker."""

    snapshot: str | None = None
    """The Daytona snapshot of each sandbox: ``piloop`` is on its ``PATH``. It fixes the sandbox's size."""
    image: str | None = None
    """An image in a registry, in place of ``snapshot``: Daytona builds a snapshot of it at its first sandbox."""
    cpu: int | None = Field(default=None, gt=0, le=4)
    """The vCPUs of each sandbox of ``image``. Unset: Daytona's default."""
    memory_gib: int | None = Field(default=None, gt=0)
    """The GiB of memory of each sandbox of ``image``, a hard limit. Unset: Daytona's default."""
    disk_gib: int | None = Field(default=None, gt=0)
    """The GiB of disk of each sandbox of ``image``. Unset: Daytona's default."""
    ttl_hours: float = Field(default=24, gt=0)
    """The longest that a sandbox lives: Daytona deletes it then, whatever it runs."""
    secrets: dict[str, str] = {}
    """Daytona Secrets of each sandbox, by the variable that holds each one's placeholder: ``{ HF_TOKEN = "hf-read" }``."""
    env: dict[str, str] = {}
    """More environment of each sandbox, such as ``PILOOP_PLUGINS``: never a secret, which Daytona's API shows."""

    @model_validator(mode='after')
    def _one_source(self) -> Self:
        if (self.snapshot is None) == (self.image is None):
            raise ValueError('name a snapshot or an image, not both')
        if self.snapshot is not None and (self.cpu, self.memory_gib, self.disk_gib) != (None, None, None):
            raise ValueError('the snapshot fixes the size of its sandboxes: cpu, memory_gib and disk_gib need an image')
        return self

    def open(self, host: ProvisionerHost) -> DaytonaProvisioning:
        return DaytonaProvisioning(self, host.owner)

    def checks(self) -> list[Check]:
        """The ``daytona`` extra, and Daytona's key in the environment."""
        if os.environ.get(KEY):
            login = Check('daytona login', f'{KEY} is set')
        else:
            login = Check('daytona login', f'{KEY} is not set', f'set {KEY} where the daemon runs')
        return [extra('daytona', 'daytona', 'daytona'), login]


class DaytonaProvisioning:
    """``Provisioning``: the sandboxes labelled with this server's owner."""

    def __init__(self, config: Daytona, owner: str) -> None:
        try:  # here, not at the top: the extra is optional, and only a pool on Daytona needs it
            import daytona
        except ImportError as e:
            raise ImportError(
                "provider = 'daytona' needs the daytona extra: uv tool install --reinstall 'piloop[daytona]'"
            ) from e
        self._sdk = daytona
        self._config = config
        self._owner = owner
        self._client: daytona_sdk.Daytona | None = None
        states = daytona.SandboxState
        self._booting = {
            states.CREATING,
            states.RESTORING,
            states.PENDING_BUILD,
            states.BUILDING_SNAPSHOT,
            states.PULLING_SNAPSHOT,
            states.STARTING,
        }

    def client(self) -> daytona_sdk.Daytona:
        """The client, made on the first call: opening the provisioner touches no network, and needs no key."""
        if self._client is None:
            self._client = self._sdk.Daytona()
        return self._client

    def _sandboxes(self, **labels: str) -> Sequence[daytona_sdk.Sandbox]:
        """The sandboxes of this owner and ``labels``, but those that Daytona destroyed."""
        query = self._sdk.ListSandboxesQuery(labels={OWNER: self._owner, **labels})
        return [s for s in self.client().list(query) if s.state is not self._sdk.SandboxState.DESTROYED]

    def _state(self, sandbox: daytona_sdk.Sandbox) -> InstanceState:
        if sandbox.state is self._sdk.SandboxState.STARTED:
            return InstanceState.RUNNING
        return InstanceState.PENDING if sandbox.state in self._booting else InstanceState.ENDED

    def list(self) -> Sequence[Instance]:
        return [
            Instance(name=s.labels[NAME], pool=s.labels[POOL], state=self._state(s), handle={'id': s.id})
            for s in self._sandboxes()
        ]

    def start(self, launch: Launch) -> None:
        """Create the sandbox, and start its worker. One of its name that boots or runs is the start that came before.

        A sandbox whose worker did not start is deleted, and the error raised: the next start makes another one.

        Raises:
            StartRefused: Daytona refused what was asked (a snapshot, a size, a quota, the key).
        """
        if any(self._state(s) is not InstanceState.ENDED for s in self._sandboxes(**{NAME: launch.name})):
            return
        sdk, client = self._sdk, self.client()
        try:
            sandbox = client.create(self._params(launch), timeout=START_SECONDS)
        except (
            sdk.DaytonaBadRequestError,
            sdk.DaytonaNotFoundError,
            sdk.DaytonaUnprocessableEntityError,
            sdk.DaytonaForbiddenError,
            sdk.DaytonaAuthenticationError,
        ) as e:
            raise StartRefused(f'Daytona refused the sandbox {launch.name}: {type(e).__name__}: {e}') from e
        try:
            sandbox.fs.upload_file(_exports(launch.env), CREDENTIAL)
            sandbox.process.create_session(SESSION)
            command = f'. {CREDENTIAL} && rm {CREDENTIAL} && {shlex.join(launch.argv)}'
            sandbox.process.execute_session_command(SESSION, sdk.SessionExecuteRequest(command=command, run_async=True))
        except Exception:
            client.delete(sandbox)
            raise

    def _params(
        self, launch: Launch
    ) -> daytona_sdk.CreateSandboxFromSnapshotParams | daytona_sdk.CreateSandboxFromImageParams:
        sdk, config = self._sdk, self._config
        fields: dict[str, object] = {
            'labels': {OWNER: self._owner, NAME: launch.name, POOL: launch.pool},
            'env_vars': dict(config.env),
            'secrets': dict(config.secrets),
            'public': False,
            'ttl_minutes': math.ceil(config.ttl_hours * 60),
            'auto_stop_interval': 0,  # Daytona counts API calls as activity, not a worker that runs a job
            'ephemeral': True,  # deleted once stopped
        }
        if config.image is None:
            return sdk.CreateSandboxFromSnapshotParams.model_validate({**fields, 'snapshot': config.snapshot})
        size = sdk.Resources(cpu=config.cpu, memory=config.memory_gib, disk=config.disk_gib)
        return sdk.CreateSandboxFromImageParams.model_validate({**fields, 'image': config.image, 'resources': size})

    def stop(self, name: str) -> None:
        sdk, client = self._sdk, self.client()
        for sandbox in self._sandboxes(**{NAME: name}):
            try:
                client.delete(sandbox)
            except sdk.DaytonaNotFoundError:  # deleted since it was listed
                pass
            except sdk.DaytonaConflictError:  # its list lags a delete by seconds, and a second delete is refused
                if not self._deleted(sandbox.id):
                    raise

    def _deleted(self, sandbox_id: str) -> bool:
        """Whether Daytona destroys the sandbox, or did: its list may still show it as it was."""
        states = self._sdk.SandboxState
        try:
            return self.client().get(sandbox_id).state in {states.DESTROYING, states.DESTROYED}
        except self._sdk.DaytonaNotFoundError:
            return True


def _exports(env: dict[str, SecretStr]) -> bytes:
    """``env`` as the lines of a shell script that exports it."""
    return ''.join(f'export {k}={shlex.quote(v.get_secret_value())}\n' for k, v in env.items()).encode()
