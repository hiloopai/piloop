"""``kubernetes``: each worker of the pool is a Kubernetes Job that runs ``piloop worker`` in the image that you name.

```toml
[plugins.executor.pools.agents.provisioner.compute]
provider = "kubernetes"
namespace = "compute"
image = "registry.example.com/piloop-worker:0.6"
cpu = 2
memory_mb = 8192
ttl_hours = 9
queue = "research"
```

The image has ``piloop`` on its ``PATH``, with the harnesses and evaluator runners that the pool's jobs need. It runs as
user 65532, with a read-only root file system: ``HOME`` and ``/tmp`` are empty directories of the pod.

Each Job gets the worker's credential in a Secret that the Job owns, so a delete of the Job deletes it too. Its pod
waits for the Secret. The Job's annotations name its worker, its pool and this server's owner, and a label holds a hash
of the owner, so ``list`` finds this server's Jobs alone.

``queue`` names a Kueue ``LocalQueue``: the Job starts suspended, and runs when Kueue admits it. A suspended Job is
pending. With ``managed_by = "kueue.x-k8s.io/multikueue"``, Kueue runs the Job on another cluster. MultiKueue copies no
Secret there, so the credential must go in the Job's environment, where anyone who reads the Job reads it:
``credential_in_job_env = true`` says that you accept this.

The pod requests ``cpu``, ``memory_mb`` and ``ephemeral_gib``, and has no CPU limit. Its memory has no limit but
``memory_limit_mb``. Kubernetes ends the Job ``ttl_hours`` after it starts to run, whatever it runs.

Without ``api``, the server runs in the cluster and uses its pod's service account. With ``api``, it reads a bearer
token from ``token_file`` at each call, so a token that rotates needs no restart.
"""

import base64
import hashlib
import math
import ssl
from collections.abc import Sequence
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlencode

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

from piloop.plugins.provisioner.contract import (
    Instance,
    InstanceState,
    Launch,
    ProvisionerConfig,
    ProvisionerHost,
    StartRefused,
)

PREFIX = 'piloop.autoresearch.inc/'
"""The prefix of the annotations and the label of a Job."""
OWNER, NAME, POOL = f'{PREFIX}owner', f'{PREFIX}worker', f'{PREFIX}pool'
QUEUE = 'kueue.x-k8s.io/queue-name'
HOME = '/home/piloop'
IN_CLUSTER = Path('/var/run/secrets/kubernetes.io/serviceaccount')
"""Where a pod finds its service account's token and the cluster's CA."""
USER = 65532
DNSName = Annotated[str, Field(pattern=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', max_length=63)]


class Endpoint(BaseModel):
    """``api``: a cluster's API server, from outside the cluster."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    server: Annotated[str, Field(pattern=r'^https://')]
    """The API server's URL."""
    token_file: Path
    """A file that holds a bearer token, read at each call."""
    ca_file: Path | None = None
    """The CA bundle that signs the server's certificate. Unset: the system's CAs."""


class Kubernetes(ProvisionerConfig):
    """``provider = "kubernetes"``: a Kubernetes Job for each worker."""

    namespace: DNSName
    """The namespace of the Jobs."""
    image: Annotated[str, Field(min_length=1)]
    """The image of each worker: ``piloop`` is on its ``PATH``, and it runs as a user that is not root."""
    cpu: float = Field(gt=0)
    """The CPUs that each pod requests. It has no CPU limit."""
    memory_mb: int = Field(gt=0)
    """The MiB of memory that each pod requests."""
    memory_limit_mb: int | None = Field(default=None, gt=0)
    """The MiB of memory past which Kubernetes kills the pod. Unset: no limit."""
    ephemeral_gib: int | None = Field(default=None, gt=0)
    """The GiB of local disk that each pod requests. Unset: no request."""
    ttl_hours: float = Field(default=24, gt=0)
    """The longest that a Job runs: Kubernetes ends it then, whatever it runs. A time in Kueue's queue does not count."""
    queue: DNSName | None = None
    """The Kueue ``LocalQueue`` of the Jobs. Unset: no Kueue, and each Job runs at once."""
    managed_by: str | None = None
    """The Job's ``managedBy``, such as ``kueue.x-k8s.io/multikueue``: another controller runs it. It needs ``queue``."""
    credential_in_job_env: bool = False
    """Put the worker's credential in the Job's environment, not in a Secret: anyone who reads the Job reads it.
    ``managed_by`` needs it, because MultiKueue copies no Secret."""
    api: Endpoint | None = None
    """The API server, from outside the cluster. Unset: the server's own pod's service account, in the cluster."""
    service_account: DNSName = 'default'
    """The service account of each pod. Its token is not mounted."""
    image_pull_secrets: list[DNSName] = []
    """The Secrets that pull ``image``."""
    node_selector: dict[str, str] = {}
    """The node labels that each pod needs, such as a CPU family for a timed task."""
    pod_labels: dict[str, str] = {}
    """More labels of each pod, such as the one that selects a network policy."""
    env: dict[str, str] = {}
    """More environment of each worker, such as ``PILOOP_PLUGINS``: never a secret, which the Job shows."""

    @model_validator(mode='after')
    def _coherent(self) -> Self:
        if self.memory_limit_mb is not None and self.memory_limit_mb < self.memory_mb:
            raise ValueError('memory_limit_mb is below memory_mb')
        if self.managed_by is not None and self.queue is None:
            raise ValueError('managed_by needs a queue: Kueue dispatches the Job')
        if self.managed_by is not None and not self.credential_in_job_env:
            raise ValueError(
                'MultiKueue copies no Secret to the cluster that runs the Job: set credential_in_job_env = true to'
                " put the worker's credential in the Job's environment, where anyone who reads the Job reads it"
            )
        if reserved := {'HOME', 'PILOOP_HOME'} & set(self.env):
            raise ValueError(f'env sets {", ".join(sorted(reserved))}, which the provider sets')
        return self

    def open(self, host: ProvisionerHost) -> Jobs:
        """The Jobs of ``host.owner``. It reads no file and calls no server until its first call."""
        return Jobs(self, host.owner, API(self.api))


class APIError(RuntimeError):
    """A call that the API server answered with an error. It names the call, its status and reason, and no body."""

    def __init__(self, method: str, path: str, status: int, reason: str = '') -> None:
        self.status = status
        super().__init__(f'Kubernetes {method} {path}: HTTP {status} {reason}'.rstrip())


class API:
    """The calls of the Job lifecycle to one API server, with a token that it reads afresh at each call."""

    def __init__(self, endpoint: Endpoint | None) -> None:
        self.endpoint = endpoint or Endpoint(
            server='https://kubernetes.default.svc', token_file=IN_CLUSTER / 'token', ca_file=IN_CLUSTER / 'ca.crt'
        )
        self._client: httpx.Client | None = None

    def client(self) -> httpx.Client:
        """The client, made on the first call: opening the provisioner reads no file."""
        if self._client is None:
            ca = self.endpoint.ca_file
            self._client = httpx.Client(
                base_url=self.endpoint.server,
                verify=ssl.create_default_context(cafile=None if ca is None else str(ca)),
                timeout=httpx.Timeout(30, connect=10),
                trust_env=False,
                follow_redirects=False,  # a redirect would carry the token elsewhere
            )
        return self._client

    def request(self, method: str, path: str, body: JsonValue = None) -> bytes:
        """The body of the answer to one call.

        Raises:
            APIError: the server answered with an error.
        """
        token = self.endpoint.token_file.read_text().strip()
        response = self.client().request(method, path, headers={'Authorization': f'Bearer {token}'}, json=body)
        if not response.is_success:
            try:
                reason = str(response.json().get('reason', ''))
            except ValueError, AttributeError:  # not a Kubernetes Status: the status alone says it
                reason = ''
            raise APIError(method, path, response.status_code, reason)
        return response.content


class OwnerReference(BaseModel):
    """An object that owns this one: a Secret's Job."""

    kind: str
    uid: str
    controller: bool = False


class Metadata(BaseModel):
    """An object's name, uid, labels, annotations and owners, and whether it is being deleted."""

    name: str
    uid: str
    annotations: dict[str, str] = {}
    owner_references: list[OwnerReference] = Field(default=[], alias='ownerReferences')
    deletion_timestamp: str | None = Field(default=None, alias='deletionTimestamp')


class Condition(BaseModel):
    """One condition of a Job, such as ``Complete`` or ``Failed``."""

    type: str
    status: str


class JobStatus(BaseModel):
    """How many of a Job's pods are ready, and its conditions."""

    ready: int = 0
    conditions: list[Condition] = []


class Job(BaseModel):
    """A Job, as ``list`` reads it."""

    metadata: Metadata
    status: JobStatus = JobStatus()

    @property
    def state(self) -> InstanceState:
        """Deleting or finished: ended. A ready pod: running. Else pending, as a Job that Kueue keeps suspended is."""
        if self.metadata.deletion_timestamp is not None or any(
            c.status == 'True' and c.type in {'Complete', 'Failed'} for c in self.status.conditions
        ):
            return InstanceState.ENDED
        return InstanceState.RUNNING if self.status.ready else InstanceState.PENDING


class ListMetadata(BaseModel):
    """Where the next page of a list starts."""

    continuation: str = Field(default='', alias='continue')


class JobList(BaseModel):
    """A page of Jobs."""

    items: list[Job]
    metadata: ListMetadata = ListMetadata()


class Secret(BaseModel):
    """A Secret, as its owner is checked: never its data."""

    metadata: Metadata


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:40]


class Jobs:
    """``Provisioning``: the Jobs of this server's owner. It checks a Job's owner before it changes the Job."""

    def __init__(self, config: Kubernetes, owner: str, api: API) -> None:
        self.config, self.owner, self.api = config, owner, api
        self.jobs = f'/apis/batch/v1/namespaces/{config.namespace}/jobs'
        self.secrets = f'/api/v1/namespaces/{config.namespace}/secrets'

    def _name(self, worker: str) -> str:
        return 'piloop-' + _hash(f'{self.owner}:{worker}')

    def _owned(self, metadata: Metadata, worker: str | None = None) -> bool:
        name = metadata.annotations.get(NAME)
        return (
            metadata.annotations.get(OWNER) == self.owner
            and name is not None
            and metadata.name == self._name(name)
            and bool(metadata.annotations.get(POOL))
            and worker in {None, name}
        )

    def list(self) -> Sequence[Instance]:
        found: list[Instance] = []
        continuation = ''
        while True:
            query = urlencode({'labelSelector': f'{OWNER}={_hash(self.owner)}', 'continue': continuation})
            page = JobList.model_validate_json(self.api.request('GET', f'{self.jobs}?{query}'))
            found += [
                Instance(
                    name=job.metadata.annotations[NAME],
                    pool=job.metadata.annotations[POOL],
                    state=job.state,
                    handle={'namespace': self.config.namespace, 'job': job.metadata.name, 'uid': job.metadata.uid},
                )
                for job in page.items
                if self._owned(job.metadata)
            ]
            if not (continuation := page.metadata.continuation):
                return found

    def start(self, launch: Launch) -> None:
        """Create the Job, then its Secret. A Job of this name is the start that came before: its Secret follows.

        A Job whose Secret could not be made is deleted, and the error raised: the next start makes another one.

        Raises:
            StartRefused: the API server refused the Job (a quota, a policy, the token), or one of another owner, or a
                Secret of another owner, has its name.
        """
        if overlap := set(launch.env) & {'HOME', 'PILOOP_HOME', *self.config.env}:
            raise StartRefused(f'the credential sets {", ".join(sorted(overlap))}, which env sets')
        job = self._create(launch)
        if self.config.credential_in_job_env:
            return
        try:
            self._secret(job, launch)
        except Exception as e:
            try:
                self._delete(job)
            except Exception as cleanup:  # the error that matters is the Secret's: this one goes with it
                e.add_note(f'and its Job {job.metadata.name} was not deleted: {type(cleanup).__name__}: {cleanup}')
            raise

    def _create(self, launch: Launch) -> Job:
        name = self._name(launch.name)
        try:
            return Job.model_validate_json(self.api.request('POST', self.jobs, self._manifest(launch)))
        except APIError as e:
            if e.status != HTTPStatus.CONFLICT:
                if 400 <= e.status < 500 and e.status != HTTPStatus.TOO_MANY_REQUESTS:
                    raise StartRefused(str(e)) from e
                raise
        job = self._get(name)
        if job is None:
            raise RuntimeError(f'the Job {name} exists, then does not: it was deleted since')
        if not self._owned(job.metadata, launch.name) or job.metadata.annotations[POOL] != launch.pool:
            raise StartRefused(f'the Job {name} is not this owner and pool')
        if job.metadata.deletion_timestamp is not None:
            raise RuntimeError(f'the Job {name} is still being deleted')
        return job

    def _get(self, name: str) -> Job | None:
        try:
            return Job.model_validate_json(self.api.request('GET', f'{self.jobs}/{name}'))
        except APIError as e:
            if e.status == HTTPStatus.NOT_FOUND:
                return None
            raise

    def _secret(self, job: Job, launch: Launch) -> None:
        """Create the Job's Secret. One of its name must be the Job's own: a start that came before made it."""
        name = job.metadata.name
        try:
            self.api.request('POST', self.secrets, self._secret_manifest(job, launch))
            return
        except APIError as e:
            if e.status != HTTPStatus.CONFLICT:
                raise
        try:
            secret = Secret.model_validate_json(self.api.request('GET', f'{self.secrets}/{name}'))
        except ValidationError:  # its message would quote the Secret's data
            raise ValueError(f'the Secret {name} is not a Secret') from None
        if not any(
            ref.kind == 'Job' and ref.uid == job.metadata.uid and ref.controller
            for ref in secret.metadata.owner_references
        ):
            raise StartRefused(f"the Secret {name} is not its Job's")

    def stop(self, name: str) -> None:
        job = self._get(self._name(name))
        if job is None:
            return
        if not self._owned(job.metadata, name):
            raise RuntimeError(f"the Job {job.metadata.name} is not this owner's: not deleted")
        self._delete(job)

    def _delete(self, job: Job) -> None:
        """Delete this Job, by its uid, and its pods and Secret before it: a Job that replaced it stays."""
        try:
            self.api.request(
                'DELETE',
                f'{self.jobs}/{job.metadata.name}',
                {
                    'apiVersion': 'v1',
                    'kind': 'DeleteOptions',
                    'propagationPolicy': 'Foreground',
                    'preconditions': {'uid': job.metadata.uid},
                },
            )
        except APIError as e:
            if e.status != HTTPStatus.NOT_FOUND:
                raise

    def _secret_manifest(self, job: Job, launch: Launch) -> dict[str, JsonValue]:
        return {
            'apiVersion': 'v1',
            'kind': 'Secret',
            'metadata': {
                'name': job.metadata.name,
                'annotations': dict(job.metadata.annotations),
                'ownerReferences': [
                    {
                        'apiVersion': 'batch/v1',
                        'kind': 'Job',
                        'name': job.metadata.name,
                        'uid': job.metadata.uid,
                        'controller': True,
                        'blockOwnerDeletion': True,
                    }
                ],
            },
            'immutable': True,
            'type': 'Opaque',
            'data': {k: base64.b64encode(v.get_secret_value().encode()).decode() for k, v in launch.env.items()},
        }

    def _manifest(self, launch: Launch) -> dict[str, JsonValue]:
        config, name = self.config, self._name(launch.name)
        labels: dict[str, JsonValue] = {OWNER: _hash(self.owner)}
        env = {'HOME': HOME, 'PILOOP_HOME': HOME, **config.env}
        if config.credential_in_job_env:
            env |= {k: v.get_secret_value() for k, v in launch.env.items()}
        requests: dict[str, JsonValue] = {'cpu': f'{config.cpu:g}', 'memory': f'{config.memory_mb}Mi'}
        if config.ephemeral_gib is not None:
            requests['ephemeral-storage'] = f'{config.ephemeral_gib}Gi'
        limits: dict[str, JsonValue] = {}
        if config.memory_limit_mb is not None:
            limits['memory'] = f'{config.memory_limit_mb}Mi'
        container: dict[str, JsonValue] = {
            'name': 'worker',
            'image': config.image,
            'command': list(launch.argv),
            'env': [{'name': k, 'value': v} for k, v in env.items()],
            'resources': {'requests': requests, 'limits': limits},
            'securityContext': {
                'allowPrivilegeEscalation': False,
                'readOnlyRootFilesystem': True,
                'capabilities': {'drop': ['ALL']},
            },
            'volumeMounts': [{'name': 'home', 'mountPath': HOME}, {'name': 'tmp', 'mountPath': '/tmp'}],
        }
        if not config.credential_in_job_env:
            container['envFrom'] = [{'secretRef': {'name': name}}]
        spec: dict[str, JsonValue] = {
            'suspend': config.queue is not None,  # Kueue unsuspends it when it admits it
            'backoffLimit': 0,
            'activeDeadlineSeconds': math.ceil(config.ttl_hours * 3600),
            'template': {
                'metadata': {'labels': {**config.pod_labels, **labels}},
                'spec': {
                    'serviceAccountName': config.service_account,
                    'automountServiceAccountToken': False,
                    'restartPolicy': 'Never',
                    'imagePullSecrets': [{'name': s} for s in config.image_pull_secrets],
                    'nodeSelector': dict(config.node_selector),
                    'securityContext': {
                        'runAsNonRoot': True,
                        'runAsUser': USER,
                        'runAsGroup': USER,
                        'fsGroup': USER,
                        'seccompProfile': {'type': 'RuntimeDefault'},
                    },
                    'containers': [container],
                    'volumes': [{'name': 'home', 'emptyDir': {}}, {'name': 'tmp', 'emptyDir': {}}],
                },
            },
        }
        if config.managed_by is not None:
            spec['managedBy'] = config.managed_by
        if config.queue is not None:
            labels = {**labels, QUEUE: config.queue}
        return {
            'apiVersion': 'batch/v1',
            'kind': 'Job',
            'metadata': {
                'name': name,
                'labels': labels,
                'annotations': {OWNER: self.owner, NAME: launch.name, POOL: launch.pool},
            },
            'spec': spec,
        }
