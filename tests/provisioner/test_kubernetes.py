"""The ``kubernetes`` provisioner, against a stand-in for an API server behind httpx's mock transport: no cluster, and
the conformance check of every provisioner."""

import json
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from itertools import count
from pathlib import Path
from urllib.parse import parse_qs

import httpx2 as httpx
import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from piloop.otel import Tracer
from piloop.plugins.provisioner.contract import PROVISIONERS, InstanceState, Launch, ProvisionerHost, StartRefused
from piloop.plugins.provisioner.kit.conformance import check_provisioning
from piloop.plugins.provisioner.kubernetes import (
    API,
    IN_CLUSTER,
    NAME,
    OWNER,
    POOL,
    QUEUE,
    APIError,
    Endpoint,
    Jobs,
    Kubernetes,
)

LAUNCH = Launch(
    name='agents-0123abcd',
    pool='agents',
    argv=['piloop', 'worker', '--server', 'https://piloop.test', '--pool', 'agents'],
    env={'PILOOP_HTTPS_TOKEN': SecretStr('plt_secret')},
)
type Doc = dict[str, JsonValue]
type Open = Callable[..., Jobs]


def at(value: JsonValue, *path: str | int) -> JsonValue:
    """The value at ``path`` in a JSON document."""
    for key in path:
        match value, key:
            case dict(), str():
                value = value[key]
            case list(), int():
                value = value[key]
            case _:
                raise AssertionError(f'no {key!r} in {value!r}')
    return value


def obj(value: JsonValue, *path: str | int) -> Doc:
    """The object at ``path`` in a JSON document."""
    found = at(value, *path)
    assert isinstance(found, dict)
    return found


@dataclass
class Cluster:
    """An API server's Jobs and Secrets in one namespace, with uid preconditions and the garbage collector's deletes."""

    jobs: dict[str, Doc] = field(default_factory=dict)
    secrets: dict[str, Doc] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    """Each call: its method and its kind of object."""
    fails: dict[tuple[str, str], int] = field(default_factory=dict)
    """The next call of this method on this kind is answered with this status, and does nothing."""
    loses: set[tuple[str, str]] = field(default_factory=set)
    """The next call of this method on this kind does what it asks, and its answer is lost."""
    page: int = 100
    lingers: bool = False
    """A deleted Job stays, being deleted, as a foreground delete keeps it while its pods end."""
    uids: count[int] = field(default_factory=lambda: count(1))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split('/')
        kind = 'jobs' if 'jobs' in parts else 'secrets'
        call = (request.method, kind)
        self.calls.append(call)
        if (status := self.fails.pop(call, None)) is not None:
            return status_of(status, 'Injected')
        response = self._answer(
            request, self.jobs if kind == 'jobs' else self.secrets, parts[-1] if parts[-1] != kind else None
        )
        if call in self.loses:
            self.loses.remove(call)
            raise httpx.ReadTimeout('the answer was lost', request=request)
        return response

    def _answer(self, request: httpx.Request, objects: dict[str, Doc], name: str | None) -> httpx.Response:
        body: JsonValue = json.loads(request.content) if request.content else None
        match request.method, name:
            case 'POST', None:
                document = deepcopy(obj(body))
                name = str(at(document, 'metadata', 'name'))
                if name in objects:
                    return status_of(409, 'AlreadyExists')
                obj(document, 'metadata')['uid'] = f'uid-{next(self.uids)}'
                objects[name] = document
                return httpx.Response(201, json=document)
            case 'GET', None:
                query = parse_qs(request.url.query.decode())
                key, _, value = query['labelSelector'][0].partition('=')
                matching = [d for d in objects.values() if obj(d, 'metadata', 'labels').get(key) == value]
                start = int(query.get('continue', ['0'])[0])
                more = len(matching) > start + self.page
                page = {'continue': str(start + self.page)} if more else {}
                return httpx.Response(200, json={'items': matching[start : start + self.page], 'metadata': page})
            case 'GET', str() if name in objects:
                return httpx.Response(200, json=objects[name])
            case 'DELETE', str() if name in objects:
                if at(objects[name], 'metadata', 'uid') != at(body, 'preconditions', 'uid'):
                    return status_of(409, 'Conflict')
                assert at(body, 'propagationPolicy') == 'Foreground'
                self._delete(name)
                return httpx.Response(200, json={'kind': 'Status', 'status': 'Success'})
            case _:
                return status_of(404, 'NotFound')

    def _delete(self, name: str) -> None:
        if self.lingers:
            obj(self.jobs[name], 'metadata')['deletionTimestamp'] = '2026-11-07T00:00:00Z'
            return
        uid = at(self.jobs.pop(name), 'metadata', 'uid')
        for key, secret in list(self.secrets.items()):
            if at(secret, 'metadata', 'ownerReferences', 0, 'uid') == uid:
                del self.secrets[key]

    def job(self) -> Doc:
        [job] = self.jobs.values()
        return job

    def container(self) -> Doc:
        return obj(self.job(), 'spec', 'template', 'spec', 'containers', 0)


def status_of(code: int, reason: str) -> httpx.Response:
    return httpx.Response(code, json={'kind': 'Status', 'reason': reason, 'message': 'details that may quote a body'})


def answered(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> None:
    """Each client that the provider makes sends its calls to ``handler``."""
    monkeypatch.setattr(httpx, 'Client', partial(httpx.Client, transport=httpx.MockTransport(handler)))


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> Cluster:
    cluster = Cluster()
    answered(monkeypatch, cluster)
    return cluster


@pytest.fixture
def token(tmp_path: Path) -> Path:
    path = tmp_path / 'token'
    path.write_text('first\n')
    return path


def config(**keys: object) -> Kubernetes:
    validated = PROVISIONERS.validated(
        {
            'provider': 'kubernetes',
            'namespace': 'compute',
            'image': 'registry.test/piloop-worker:0.6',
            'cpu': 2,
            'memory_mb': 8192,
            **keys,
        }
    )
    assert isinstance(validated, Kubernetes)
    return validated


@pytest.fixture
def opened(cluster: Cluster, token: Path) -> Open:
    def opened(owner: str, **keys: object) -> Jobs:
        api = {'server': 'https://k8s.test', 'token_file': str(token)}
        return config(api=api, **keys).open(ProvisionerHost(owner, Tracer(None)))

    return opened


def test_kubernetes_passes_the_provisioner_conformance_check(opened: Open) -> None:
    check_provisioning(opened, LAUNCH, timeout=1)


def test_without_api_the_server_uses_its_pods_service_account() -> None:
    assert config().open(ProvisionerHost('o', Tracer(None))).api.endpoint == Endpoint(
        server='https://kubernetes.default.svc', token_file=IN_CLUSTER / 'token', ca_file=IN_CLUSTER / 'ca.crt'
    )


def test_a_job_runs_the_argv_with_the_credential_in_a_secret_that_it_owns(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a', env={'PILOOP_TICK_SECONDS': '2'}, ttl_hours=9, node_selector={'cpu-family': 'zen4'})
    jobs.start(LAUNCH)
    job, [secret] = cluster.job(), cluster.secrets.values()
    assert 'plt_secret' not in json.dumps(job), 'the credential is not in the Job, which anyone who reads Jobs reads'
    assert (secret['data'], secret['immutable']) == ({'PILOOP_HTTPS_TOKEN': 'cGx0X3NlY3JldA=='}, True)
    assert at(secret, 'metadata', 'ownerReferences', 0, 'uid') == at(job, 'metadata', 'uid')
    assert at(job, 'metadata', 'annotations') == {OWNER: 'owner-a', NAME: LAUNCH.name, POOL: 'agents'}
    assert QUEUE not in obj(job, 'metadata', 'labels') and at(job, 'spec', 'suspend') is False, 'no queue: it runs'
    assert 'managedBy' not in obj(job, 'spec') and at(job, 'spec', 'activeDeadlineSeconds') == 9 * 3600
    pod = obj(job, 'spec', 'template', 'spec')
    assert (pod['automountServiceAccountToken'], pod['nodeSelector']) == (False, {'cpu-family': 'zen4'})
    container = cluster.container()
    assert container['command'] == LAUNCH.argv
    assert container['envFrom'] == [{'secretRef': {'name': at(job, 'metadata', 'name')}}]
    assert container['env'] == [
        {'name': 'HOME', 'value': '/home/piloop'},
        {'name': 'PILOOP_HOME', 'value': '/home/piloop'},
        {'name': 'PILOOP_TICK_SECONDS', 'value': '2'},
    ]
    assert container['resources'] == {'requests': {'cpu': '2', 'memory': '8192Mi'}, 'limits': {}}, 'no hard limit'
    [instance] = jobs.list()
    assert (instance.name, instance.pool, instance.state) == (LAUNCH.name, 'agents', InstanceState.PENDING)
    assert instance.handle == {'namespace': 'compute', 'job': at(job, 'metadata', 'name'), 'uid': 'uid-1'}


def test_a_pod_requests_its_disk_and_takes_a_memory_limit_and_labels_when_set(opened: Open, cluster: Cluster) -> None:
    keys = {'cpu': 0.5, 'memory_limit_mb': 12288, 'ephemeral_gib': 20, 'pod_labels': {'egress': 'gateway', OWNER: 'x'}}
    opened('owner-a', **keys).start(LAUNCH)
    assert cluster.container()['resources'] == {
        'requests': {'cpu': '0.5', 'memory': '8192Mi', 'ephemeral-storage': '20Gi'},
        'limits': {'memory': '12288Mi'},
    }
    job = cluster.job()
    assert at(job, 'spec', 'template', 'metadata', 'labels') == {
        'egress': 'gateway',
        OWNER: at(job, 'metadata', 'labels', OWNER),
    }, 'a pod label never hides the owner'


def test_a_queued_job_starts_suspended_for_kueue_and_is_pending_until_a_pod_is_ready(
    opened: Open, cluster: Cluster
) -> None:
    jobs = opened('owner-a', queue='research')
    jobs.start(LAUNCH)
    job = cluster.job()
    assert at(job, 'spec', 'suspend') is True and at(job, 'metadata', 'labels', QUEUE) == 'research'
    assert QUEUE not in obj(job, 'spec', 'template', 'metadata', 'labels')
    assert [i.state for i in jobs.list()] == [InstanceState.PENDING], 'queued, not a failed start'
    obj(job, 'spec')['suspend'] = False
    job['status'] = {'active': 1, 'ready': 1}
    assert [i.state for i in jobs.list()] == [InstanceState.RUNNING]
    job['status'] = {'active': 1, 'ready': 1, 'conditions': [{'type': 'Complete', 'status': 'False'}]}
    assert [i.state for i in jobs.list()] == [InstanceState.RUNNING]
    job['status'] = {'conditions': [{'type': 'Failed', 'status': 'True'}]}
    assert [i.state for i in jobs.list()] == [InstanceState.ENDED]


def test_multikueue_puts_the_credential_in_the_jobs_env_only_when_told_to(opened: Open, cluster: Cluster) -> None:
    keys = {'queue': 'research', 'managed_by': 'kueue.x-k8s.io/multikueue'}
    with pytest.raises(ValidationError, match='credential_in_job_env'):
        config(**keys)
    with pytest.raises(ValidationError, match='needs a queue'):
        config(managed_by='kueue.x-k8s.io/multikueue', credential_in_job_env=True)
    opened('owner-a', credential_in_job_env=True, **keys).start(LAUNCH)
    assert at(cluster.job(), 'spec', 'managedBy') == 'kueue.x-k8s.io/multikueue' and not cluster.secrets
    container, env = cluster.container(), at(cluster.container(), 'env')
    assert 'envFrom' not in container and isinstance(env, list)
    assert {'name': 'PILOOP_HTTPS_TOKEN', 'value': 'plt_secret'} in env


def test_a_config_that_contradicts_itself_is_refused() -> None:
    with pytest.raises(ValidationError, match='below memory_mb'):
        config(memory_limit_mb=4096)
    with pytest.raises(ValidationError, match='env sets HOME'):
        config(env={'HOME': '/root'})
    with pytest.raises(ValidationError, match='server'):
        Endpoint(server='http://k8s.test', token_file=Path('token'))


def test_a_credential_that_overlaps_the_env_is_refused_before_any_call(opened: Open, cluster: Cluster) -> None:
    with pytest.raises(StartRefused, match='PILOOP_HTTPS_TOKEN'):
        opened('owner-a', env={'PILOOP_HTTPS_TOKEN': 'stale'}).start(LAUNCH)
    assert cluster.calls == []


def test_a_job_whose_secret_fails_is_deleted_so_the_next_start_makes_another(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a')
    cluster.fails[('POST', 'secrets')] = 500
    with pytest.raises(APIError, match='HTTP 500 Injected'):
        jobs.start(LAUNCH)
    assert cluster.jobs == {} and jobs.list() == []
    cluster.loses.add(('POST', 'secrets'))
    with pytest.raises(httpx.ReadTimeout):
        jobs.start(LAUNCH)
    assert cluster.jobs == {} and cluster.secrets == {}, 'the Secret goes with its Job'
    jobs.start(LAUNCH)
    assert len(cluster.jobs) == len(cluster.secrets) == 1


def test_a_failed_cleanup_is_noted_on_the_error_that_caused_it(opened: Open, cluster: Cluster) -> None:
    cluster.fails[('POST', 'secrets')] = 500
    cluster.fails[('DELETE', 'jobs')] = 503
    with pytest.raises(APIError, match='HTTP 500') as raised:
        opened('owner-a').start(LAUNCH)
    assert any('was not deleted: APIError' in note and 'HTTP 503' in note for note in raised.value.__notes__)
    assert len(cluster.jobs) == 1


def test_a_start_whose_job_answer_was_lost_finishes_the_job_it_made(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a')
    cluster.loses.add(('POST', 'jobs'))
    with pytest.raises(httpx.ReadTimeout):
        jobs.start(LAUNCH)
    assert len(cluster.jobs) == 1 and not cluster.secrets
    jobs.start(LAUNCH)
    jobs.start(LAUNCH)
    assert len(cluster.jobs) == len(cluster.secrets) == 1
    assert cluster.calls.count(('POST', 'jobs')) == 3


def test_a_job_the_api_server_refuses_is_a_refused_start_and_its_other_errors_are_tried_again(
    opened: Open, cluster: Cluster
) -> None:
    jobs = opened('owner-a')
    for status in (400, 403, 422):
        cluster.fails[('POST', 'jobs')] = status
        with pytest.raises(StartRefused, match=f'HTTP {status}'):
            jobs.start(LAUNCH)
    for status in (429, 500):
        cluster.fails[('POST', 'jobs')] = status
        with pytest.raises(APIError) as raised:
            jobs.start(LAUNCH)
        assert not isinstance(raised.value, StartRefused)
    assert cluster.jobs == {}


def test_a_job_or_secret_of_another_owner_is_never_taken_or_deleted(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a')
    jobs.start(LAUNCH)
    annotations = obj(cluster.job(), 'metadata', 'annotations')
    annotations[OWNER] = 'owner-b'
    with pytest.raises(StartRefused, match='not this owner'):
        jobs.start(LAUNCH)
    with pytest.raises(RuntimeError, match='not deleted'):
        jobs.stop(LAUNCH.name)
    assert jobs.list() == [] and len(cluster.jobs) == 1
    annotations[OWNER] = 'owner-a'
    [secret] = cluster.secrets.values()
    obj(secret, 'metadata', 'ownerReferences', 0)['uid'] = 'uid-of-another-job'
    with pytest.raises(StartRefused, match="not its Job's"):
        jobs.start(LAUNCH)


def test_a_secret_that_is_not_one_is_refused_without_quoting_it(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a')
    jobs.start(LAUNCH)
    [secret] = cluster.secrets.values()
    del obj(secret, 'metadata')['uid']
    with pytest.raises(ValueError, match='is not a Secret') as raised:
        jobs.start(LAUNCH)
    assert raised.value.__cause__ is None and 'cGx0' not in str(raised.value)


def test_a_job_that_is_being_deleted_or_was_deleted_meanwhile_is_not_taken(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a')
    cluster.lingers = True
    jobs.start(LAUNCH)
    jobs.stop(LAUNCH.name)
    assert [i.state for i in jobs.list()] == [InstanceState.ENDED]
    with pytest.raises(RuntimeError, match='still being deleted'):
        jobs.start(LAUNCH)
    cluster.jobs.clear()
    cluster.fails[('POST', 'jobs')] = 409
    with pytest.raises(RuntimeError, match='exists, then does not'):
        jobs.start(LAUNCH)


def test_stop_deletes_only_the_job_that_it_read(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a')
    jobs.start(LAUNCH)
    cluster.fails[('DELETE', 'jobs')] = 404
    jobs.stop(LAUNCH.name)
    assert len(cluster.jobs) == 1, 'gone meanwhile: a success'
    cluster.fails[('DELETE', 'jobs')] = 409
    with pytest.raises(APIError, match='409'):
        jobs.stop(LAUNCH.name)
    cluster.fails[('GET', 'jobs')] = 500
    with pytest.raises(APIError, match='500'):
        jobs.stop(LAUNCH.name)
    jobs.stop(LAUNCH.name)
    assert cluster.jobs == {} and cluster.secrets == {}


def test_list_reads_every_page(opened: Open, cluster: Cluster) -> None:
    jobs = opened('owner-a')
    cluster.page = 2
    names = [f'agents-{i}' for i in range(5)]
    for name in names:
        jobs.start(LAUNCH.model_copy(update={'name': name}))
    assert sorted(i.name for i in jobs.list()) == names


def test_each_call_reads_the_token_afresh_and_follows_no_redirect(token: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers['Authorization'])
        return httpx.Response(302, headers={'Location': 'https://elsewhere.test'})

    answered(monkeypatch, redirect)
    api = API(Endpoint(server='https://k8s.test', token_file=token))
    for value in ('first', 'rotated'):
        token.write_text(value)
        with pytest.raises(APIError, match='HTTP 302$') as raised:
            api.request('GET', '/api/v1/namespaces/compute/secrets/s')
        assert raised.value.status == 302 and value not in str(raised.value)
    assert seen == ['Bearer first', 'Bearer rotated']


@pytest.mark.parametrize('body', [b'not json', b'[]'])
def test_an_error_that_is_not_a_status_names_its_code(
    token: Path, body: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    answered(monkeypatch, lambda request: httpx.Response(502, content=body))
    api = API(Endpoint(server='https://k8s.test', token_file=token))
    with pytest.raises(APIError, match=r'GET /x: HTTP 502$'):
        api.request('GET', '/x')


def test_a_ca_file_is_read_when_the_first_call_is_made(tmp_path: Path, token: Path) -> None:
    api = API(Endpoint(server='https://k8s.test', token_file=token, ca_file=tmp_path / 'missing.crt'))
    with pytest.raises(FileNotFoundError):
        api.client()
