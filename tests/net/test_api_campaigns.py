"""The API registers, forks, clones, syncs, retires and lists campaigns."""

import logging
import subprocess
from http import HTTPStatus
from pathlib import Path

import httpx2 as httpx
import pytest
from git import Repo
from pydantic import SecretStr
from pytest_mock import MockerFixture
from rich.console import Console
from sqlalchemy import select, update

from piloop.actor import System
from piloop.api.client import Client, Refused
from piloop.api.contract import (
    Checkout,
    Clone,
    Fork,
    InboxQuery,
    Located,
    PauseBody,
    Problem,
    Remote,
    RetireBody,
    SecretBody,
    SecretCredential,
    Selection,
    SyncBody,
    TokenCredential,
    defaulted,
)
from piloop.api.views import CampaignState, InvalidConfig
from piloop.campaign import ops
from piloop.campaign.stop import CampaignClosed
from piloop.cli import common, main as cli
from piloop.db.events import emit
from piloop.db.models import (
    AgentSession,
    AgentSessionState,
    Campaign,
    Comment,
    Event,
    Pause,
)
from piloop.db.session import Database
from piloop.db.triggers.changelog import CampaignPausedChanged, CampaignStatusChanged
from piloop.ids import CampaignStatus, CommentKind, TargetKind, TargetRef
from piloop.plugins.campaign_type.kit.candidates.ids import CandidateId
from piloop.plugins.campaign_type.kit.candidates.tables import Candidate
from piloop.plugins.campaign_type.kit.candidates.wire import ImportBody
from piloop.plugins.campaign_type.kit.evaluation.measure import MeasurementRecorded
from piloop.plugins.vcs.git import Git
from piloop.settings import Settings
from tests.conftest import EveryClient, measuring, serving_api, store_config
from tests.net.test_api import first
from tests.test_git import Basic, basic_server


def bare(checkout: Path, at: Path) -> str:
    """A bare clone of ``checkout`` at ``at``, as a file:// URL."""
    subprocess.run(['git', 'clone', '-q', '--bare', str(checkout), str(at)], check=True)
    return at.as_uri()


def test_a_checkout_registers_at_once_the_daemon_measures_its_baseline_and_its_piloop_toml_is_read_again(
    db: Database, campaign_path: Path, cfg: Settings
) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        added = client.register_campaign(Checkout(path=str(campaign_path)))
        assert (added.campaign.name, added.campaign.path, added.baseline) == (
            'demo',
            str(campaign_path),
            CandidateId(1),
        )
        assert isinstance(added.campaign, CampaignState)
        head = added.campaign.head
        assert head is not None and (head.candidate, [t.value for t in head.tracks]) == (added.baseline, [None]), (
            'the baseline is the head, and nothing measured it yet'
        )
        assert added.documents.line() == 'documents: 0 added, 0 unchanged'
        with measuring(cfg):  # the daemon's next pass, which also follows a restart
            recorded = first(client, added.cursor, *MeasurementRecorded.kinds())
        assert recorded == MeasurementRecorded(
            candidate=added.baseline, metrics={'score': 1.0, 'size': 5.0}, error=None, run=None
        )
        with pytest.raises(Refused, match=f'^{campaign_path} is the checkout of campaign demo #1 already$') as taken:
            client.register_campaign(Checkout(path=str(campaign_path), name='other'))
        assert taken.value.error == Problem.REFUSED
        with pytest.raises(Refused, match='^path: an absolute path, not campaign$'):
            client.register_campaign(Checkout.model_construct(path='campaign'))
        transport = httpx.HTTPTransport(uds=str(cfg.socket_file))
        with httpx.Client(transport=transport, base_url='http://piloop') as raw:
            for neither_or_both in ({}, {'path': str(campaign_path), 'clone': {'url': 'file:///x.git'}}):
                assert raw.post('/v1/campaigns', json=neither_or_both).status_code == HTTPStatus.UNPROCESSABLE_ENTITY
        with db.session() as s:
            s.execute(update(Campaign).values(status=CampaignStatus.STOPPED))
            s.commit()
        (campaign_path / 'piloop.toml').write_text('name = "demo"\n')
        with pytest.raises(Refused, match='is not a valid piloop config') as invalid:
            client.sync_campaign('demo')
        assert invalid.value.error == Problem.REFUSED
        subprocess.run(['git', 'checkout', '-q', 'piloop.toml'], cwd=campaign_path, check=True)
        synced = client.sync_campaign('demo')
        assert (synced.was, synced.campaign.status) == (CampaignStatus.STOPPED, CampaignStatus.STOPPED), (
            'the stop conditions did not change'
        )


def test_a_fork_starts_from_a_version_of_its_source_in_a_clone_with_its_config_and_repo(
    db: Database, campaign_path: Path, cfg: Settings
) -> None:
    repo = Repo(campaign_path)
    base = repo.head.commit.hexsha
    (campaign_path / 'solve.txt').write_text('a run of the source\n')
    repo.git.commit('-qam', 'a run')
    kept = repo.head.commit.hexsha
    repo.git.update_ref('refs/piloop/demo/r9/0', kept)
    repo.git.reset('-q', '--hard', base)  # only piloop's ref keeps the run's commit
    repo.close()
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        source = client.register_campaign(Checkout(path=str(campaign_path))).campaign
        with pytest.raises(Refused, match="^no campaign 'nope'") as unknown:
            client.register_campaign(Fork(fork='nope'))
        assert unknown.value.error == Problem.UNKNOWN_CAMPAIGN
        with pytest.raises(Refused, match="^demo has no version 'nowhere' to fork from: ") as no_version:
            client.register_campaign(Fork(fork='demo', at='nowhere'))
        assert no_version.value.error == Problem.REFUSED
        at_head = client.register_campaign(Fork(fork='demo'))
        at_run = client.register_campaign(Fork(fork=str(source.id), at=kept, name='from-the-run'))
    with db.session() as s:
        first, second = s.get_one(Campaign, at_head.campaign.id), s.get_one(Campaign, at_run.campaign.id)
        assert (first.name, first.parent_id, first.repo, first.path) == (
            'demo-2',
            source.id,
            s.get_one(Campaign, source.id).repo,
            str(cfg.home / 'repos' / str(first.id)),
        ), 'the memory of the repo is shared'
        assert first.config == s.get_one(Campaign, source.id).config, "the source's stored config"
        assert s.get_one(Candidate, at_head.baseline).revision == base, "the source's head is its baseline"
        assert (second.name, s.get_one(Candidate, at_run.baseline).revision) == ('from-the-run', kept)


def test_a_registration_names_the_campaign_and_its_git_refs_carry_that_name(
    db: Database, campaign_path: Path, cfg: Settings
) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(toml.read_text().replace('name = "demo"', 'name = "42"'))
    subprocess.run(['git', 'commit', '-qam', 'a name of digits'], cwd=campaign_path, check=True)
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(
            Refused, match="^'42' is no campaign name: a reference of digits names a campaign by its"
        ) as e:
            client.register_campaign(Checkout(path=str(campaign_path)))
        assert e.value.error == Problem.REFUSED
        added = client.register_campaign(Checkout(path=str(campaign_path), name='chosen'))
        assert added.campaign.name == 'chosen'
        (campaign_path / 'solve.txt').write_text('another\n')
        subprocess.run(['git', 'commit', '-qam', 'another tree'], cwd=campaign_path, check=True)
        imported = client.import_candidate('chosen', ImportBody(version='HEAD')).candidate
    refs = subprocess.run(
        ['git', 'for-each-ref', '--format=%(refname)'], cwd=campaign_path, capture_output=True, text=True
    )
    assert f'refs/piloop/chosen/reference/{imported.id}' in refs.stdout.split(), 'not the name of piloop.toml'


def test_a_stopped_campaign_retires_once_its_stop_is_over_and_its_name_registers_again(
    db: Database, campaign: Campaign, campaign_path: Path, cfg: Settings
) -> None:
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(
            Refused,
            match=r'^demo is active: only a stopped campaign retires; stop it first, with `piloop campaign stop demo`$',
        ) as e:
            client.retire('demo', RetireBody(reason='done'))
        assert e.value.error == Problem.REFUSED
        with db.session() as s:
            s.get_one(Campaign, campaign.id).status = CampaignStatus.STOPPED
            s.add(AgentSession(kind='research', campaign_id=campaign.id, state=AgentSessionState.RUNNING))
            s.add(
                Comment(
                    target=TargetRef(TargetKind.CAMPAIGN, campaign.id),
                    author='w',
                    kind='escalation',
                    body='?',
                    priority=1,
                )
            )
            s.commit()
        with pytest.raises(Refused, match=r'^demo is still stopping; agents #1 are not over: try again once its') as e:
            client.retire('demo', RetireBody(reason='done'))
        assert e.value.error == Problem.STILL_STOPPING
        with db.session() as s:
            s.execute(update(AgentSession).values(state=AgentSessionState.EXITED))
            s.commit()
        with pytest.raises(
            Refused, match=r'^demo is still stopping: try again once its closing review and runs are over'
        ):
            client.retire('demo', RetireBody(reason='done'))
        with db.session() as s:
            emit(s, CampaignClosed(), TargetRef(TargetKind.CAMPAIGN, campaign.id), System.DAEMON)
            s.commit()
        with pytest.raises(Refused, match='^a reason is required'):
            client.retire('demo', RetireBody(reason=' '))
        assert client.inbox(InboxQuery()).items, 'the escalation waits'
        retired = client.retire('demo', RetireBody(reason='the e2e ended')).campaign
        assert (retired.id, retired.status) == (campaign.id, CampaignStatus.RETIRED)
        assert client.inbox(InboxQuery()).items == [], 'a retired campaign asks nothing more'
        assert client.status(Selection()).campaigns == []
        [listed] = client.status(Selection(status=(CampaignStatus.RETIRED,))).campaigns
        [by_id] = client.status(Selection(campaign=str(campaign.id))).campaigns
        assert listed.name == by_id.name == 'demo'
        with pytest.raises(Refused, match=f'^campaign #{campaign.id} \\(demo\\) is retired$') as changed:
            client.pause(str(campaign.id), PauseBody(reason='no'))
        assert changed.value.error == Problem.REFUSED
        with pytest.raises(Refused, match='^no campaign .demo.') as gone:
            client.pause('demo', PauseBody(reason='no'))
        assert gone.value.error == Problem.UNKNOWN_CAMPAIGN
        again = client.register_campaign(Checkout(path=str(campaign_path))).campaign
        assert (again.name, again.path, again.status) == ('demo', str(campaign_path), CampaignStatus.ACTIVE)
        assert again.id != campaign.id
        [here] = client.status(Selection(cwd=str(campaign_path))).campaigns
        assert here.id == again.id, 'the retired campaign holds no path'
    with db.session() as s:
        notes = s.scalars(select(Comment.body).where(Comment.kind == CommentKind.NOTE)).all()
        retirements = s.scalars(select(Event.payload).where(Event.kind == 'campaign.status')).all()
        assert notes == ['retired: the e2e ended']
        assert [p.new for p in retirements if isinstance(p, CampaignStatusChanged)][-1] is CampaignStatus.RETIRED
        with pytest.raises(
            ops.Refused, match=f'^campaign #{campaign.id} \\(demo\\) is retired, for good: fork it instead'
        ):
            ops.retire(s, campaign.id, System.HUMAN, reason='twice')


def test_a_baseline_that_fails_to_evaluate_registers_and_then_pauses_its_campaign(
    db: Database, campaign_path: Path, cfg: Settings
) -> None:
    toml = campaign_path / 'piloop.toml'
    toml.write_text(toml.read_text().replace('"sh", "-c"', '"false", "-c"'))
    with serving_api(cfg), measuring(cfg), Client.on(cfg.socket_file, System.HUMAN) as client:
        added = client.register_campaign(Checkout(path=str(campaign_path)))
        paused = first(client, added.cursor, *CampaignPausedChanged.kinds())
    logs = cfg.home / 'projects' / str(added.campaign.id) / 'measure' / f'{added.baseline}-e1'
    assert isinstance(paused, CampaignPausedChanged) and paused.new is Pause.LAUNCHES
    assert paused.paused_reason == (
        f'the baseline failed to evaluate: evaluate exit 1: ; its logs are in {logs}; fix it, then'
        f' `piloop candidate remeasure {added.baseline}` and `piloop resume demo`'
    )
    with db.session() as s:
        assert s.get_one(Campaign, added.campaign.id).name == 'demo', 'it stays registered'


def test_a_git_url_is_cloned_under_the_home_with_the_home_s_secret_and_then_registered_as_often_as_asked(
    db: Database, campaign_path: Path, cfg: Settings, tmp_path: Path, mocker: MockerFixture
) -> None:
    url = bare(campaign_path, tmp_path / 'demo.git')
    cloned = mocker.spy(Git, 'clone')
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(Refused, match=r'^the home has no secret TOKEN: see `piloop secret list`$') as unknown:
            client.register_campaign(Clone(clone=Remote(url=url, credential=SecretCredential(secret='TOKEN'))))
        assert unknown.value.error == Problem.REFUSED and not (cfg.home / 'repos').exists()
        client.set_secret('TOKEN', Located(), SecretBody(value=SecretStr('tok')))
        added = client.register_campaign(Clone(clone=Remote(url=url, credential=SecretCredential(secret='TOKEN'))))
        assert added.campaign.path == str(cfg.home / 'repos' / str(added.campaign.id))
        [(_, _, path, credential)] = [c.args for c in cloned.call_args_list]
        assert (path.parent, credential.get_secret_value()) == (cfg.home / 'repos', 'tok')
        again = client.register_campaign(Clone(clone=Remote(url=url), name='again'))
        assert (again.campaign.name, again.campaign.path) == ('again', str(cfg.home / 'repos' / str(again.campaign.id)))
        (cfg.home / 'repos' / str(again.campaign.id + 1)).mkdir()
        with pytest.raises(
            Refused, match=f'^{cfg.home / "repos" / str(again.campaign.id + 1)} exists: move it away'
        ) as e:
            client.register_campaign(Clone(clone=Remote(url=url), name='third'))
        assert e.value.error == Problem.REFUSED
    assert sorted(p.name for p in (cfg.home / 'repos').iterdir()) == ['1', '2', '3'], 'the refused clone is gone'


def test_a_repo_without_a_piloop_toml_is_refused_with_how_to_make_one(
    db: Database, campaign_path: Path, cfg: Settings, tmp_path: Path
) -> None:
    repo = Repo(campaign_path)
    repo.index.remove(['piloop.toml'], working_tree=True)
    repo.index.commit('no config')
    url = bare(campaign_path, tmp_path / 'demo.git')
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(Refused) as cloned:
            client.register_campaign(Clone(clone=Remote(url=url)))
        with pytest.raises(Refused) as checkout:
            client.register_campaign(Checkout(path=str(campaign_path)))
    assert (cloned.value.error, str(cloned.value)) == (
        Problem.REFUSED,
        f'{url} has no piloop.toml at its root: `piloop init` in a checkout makes one; commit it, then register again',
    )
    assert (checkout.value.error, str(checkout.value)) == (
        Problem.REFUSED,
        f'{campaign_path} has no piloop.toml: `piloop init {campaign_path}` makes one',
    )
    assert list((cfg.home / 'repos').iterdir()) == [], 'the refused clone is gone'


def test_a_sync_takes_a_private_repository_s_new_commit_with_a_token_that_nothing_keeps_or_a_home_secret(
    db: Database, campaign_path: Path, cfg: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'clone', '-q', '--bare', str(campaign_path), str(origin)], check=True)
    token = Basic.password
    upstream = tmp_path / 'upstream'
    subprocess.run(['git', 'clone', '-q', str(origin), str(upstream)], check=True)
    caplog.set_level(logging.DEBUG)

    def push(message: str) -> str:
        """A new commit in the private repository: its sha. A dumb HTTP server serves what ``update-server-info`` lists."""
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', message], cwd=upstream, check=True)
        subprocess.run(['git', 'push', '-q'], cwd=upstream, check=True)
        subprocess.run(['git', 'update-server-info'], cwd=origin, check=True)
        return Repo(upstream).head.commit.hexsha

    with basic_server(origin) as url, serving_api(cfg), Client.on(cfg.socket_file, System.HUMAN) as client:
        added = client.register_campaign(
            Clone(clone=Remote(url=url, credential=TokenCredential(token=SecretStr(token))))
        )
        checkout, ref = Repo(added.campaign.path), str(added.campaign.id)
        new = push('upstream moved')
        with pytest.raises(Refused, match=r'^git pull in .* failed: ') as unreachable:
            client.sync_campaign(ref)
        assert unreachable.value.error == Problem.REFUSED
        client.sync_campaign(ref, SyncBody(credential=TokenCredential(token=SecretStr(token))))
        assert checkout.head.commit.hexsha == new, 'the token fetched the new commit'
        transport = httpx.HTTPTransport(uds=str(cfg.socket_file))
        with httpx.Client(transport=transport, base_url='http://piloop') as raw:
            sync = f'/v1/campaigns/{ref}/sync'
            for invalid in (
                {'credential': {'token': token}, 'secret': 'TOKEN'},
                {'credential': {'token': token, 'secret': 'TOKEN'}},
                {'credential': {}},
            ):
                answer = raw.post(sync, json=invalid)
                assert answer.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, invalid
                assert token not in answer.text, 'a refusal does not send the token back'
        with db.session() as s:
            assert not [e for e in s.scalars(select(Event)) if token in e.payload.model_dump_json()]
        kept = [p for p in cfg.home.rglob('*') if p.is_file() and token.encode() in p.read_bytes()]
        assert kept == [], 'no file of the home holds it: not the database, its WAL, the CAS or the clone'
        assert token not in caplog.text
        client.set_secret('TOKEN', Located(), SecretBody(value=SecretStr(token)))
        newer = push('upstream moved again')
        client.sync_campaign(ref, SyncBody(credential=SecretCredential(secret='TOKEN')))
        assert checkout.head.commit.hexsha == newer, "the home's secret fetched it"


def test_a_sync_of_a_campaign_registered_by_path_refuses_a_credential(
    db: Database, campaign_path: Path, cfg: Settings
) -> None:
    with serving_api(cfg), Client.on(cfg.socket_file, System.HUMAN) as client:
        client.register_campaign(Checkout(path=str(campaign_path)))
        for credential in (SecretCredential(secret='TOKEN'), TokenCredential(token=SecretStr('tok'))):
            with pytest.raises(Refused, match=r'^campaign demo is a checkout of .*: a credential is for a cloned') as e:
                client.sync_campaign('demo', SyncBody(credential=credential))
            assert e.value.error == Problem.REFUSED
        assert client.sync_campaign('demo').campaign.name == 'demo', 'without one, it syncs'


def test_a_clone_with_a_token_uses_it_for_that_clone_and_no_file_of_the_home_or_answer_keeps_it(
    db: Database, campaign_path: Path, cfg: Settings, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A token that the call carries opens a private repo, and is gone with the call: on disk, in logs and in answers."""
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'clone', '-q', '--bare', str(campaign_path), str(origin)], check=True)
    token = Basic.password
    caplog.set_level(logging.DEBUG)
    with basic_server(origin) as url, serving_api(cfg), Client.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(Refused, match=f'^git clone of {url} failed: ') as refused:
            client.register_campaign(Clone(clone=Remote(url=url, credential=TokenCredential(token=SecretStr('wrong')))))
        assert refused.value.error == Problem.REFUSED
        added = client.register_campaign(
            Clone(clone=Remote(url=url, credential=TokenCredential(token=SecretStr(token))))
        )
        assert added.campaign.path == str(cfg.home / 'repos' / str(added.campaign.id))
        transport = httpx.HTTPTransport(uds=str(cfg.socket_file))
        with httpx.Client(transport=transport, base_url='http://piloop') as raw:
            for invalid in (
                {'clone': {'url': url, 'credential': {'token': token, 'secret': 'TOKEN'}}},
                {'clone': {'url': url, 'credential': {'token': token}, 'secret': 'TOKEN'}},
                {'clone': {'url': url, 'credential': {'token': token}}, 'name': 'not a name'},
                {'clone': {'url': url, 'credential': {}}},
            ):
                answer = raw.post('/v1/campaigns', json=invalid)
                assert answer.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, invalid
                assert token not in answer.text, 'a refusal does not send the token back'
        with db.session() as s:
            assert not [e for e in s.scalars(select(Event)) if token in e.payload.model_dump_json()]
    kept = [p for p in cfg.home.rglob('*') if p.is_file() and token.encode() in p.read_bytes()]
    assert kept == [], 'no file of the home holds it: not the database, its WAL, the CAS or the clone'
    assert token not in caplog.text


def test_a_sync_of_a_cloned_campaign_takes_its_repository_s_new_versions_and_reads_its_piloop_toml_again(
    db: Database, campaign_path: Path, cfg: Settings, tmp_path: Path
) -> None:
    url = bare(campaign_path, tmp_path / 'demo.git')
    with serving_api(cfg), Client.on(cfg.socket_file, System.HUMAN) as client:
        added = client.register_campaign(Clone(clone=Remote(url=url)))
        upstream = tmp_path / 'upstream'
        subprocess.run(['git', 'clone', '-q', url, str(upstream)], check=True)
        toml = upstream / 'piloop.toml'
        toml.write_text(toml.read_text().replace('"sh", "-c"', '"bash", "-c"'))
        subprocess.run(['git', 'commit', '-qam', 'evaluate with bash'], cwd=upstream, check=True)
        subprocess.run(['git', 'push', '-q'], cwd=upstream, check=True)
        synced = client.sync_campaign(str(added.campaign.id), SyncBody())
        checkout = Path(added.campaign.path)
        assert 'bash' in (checkout / 'piloop.toml').read_text(), 'the checkout moved forward'
        with db.session() as s:
            row = s.get_one(Campaign, added.campaign.id)
            assert row.config.evaluate[0] == 'bash' and row.evaluator_id != 1, 'a new evaluator'
            assert s.scalars(select(Candidate.id).where(Candidate.campaign_id == row.id)).all() == [added.baseline]
        assert synced.campaign.status == 'active'
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'its own way'], cwd=checkout, check=True)
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'upstream'], cwd=upstream, check=True)
        subprocess.run(['git', 'push', '-q'], cwd=upstream, check=True)
        with pytest.raises(Refused, match=r'^git pull in .* failed: ') as diverged:
            client.sync_campaign(str(added.campaign.id))
        assert diverged.value.error == Problem.REFUSED


def test_a_clone_that_fails_or_does_not_register_leaves_nothing_under_the_home(
    db: Database, campaign_path: Path, cfg: Settings, tmp_path: Path
) -> None:
    url = bare(campaign_path, tmp_path / 'taken.git')
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        with pytest.raises(Refused, match='^git clone of file:///nowhere.git failed: ') as failed:
            client.register_campaign(Clone(clone=Remote(url='file:///nowhere.git')))
        assert failed.value.error == Problem.REFUSED
        client.register_campaign(Checkout(path=str(campaign_path)))
        with pytest.raises(Refused, match="^campaign name 'demo' is already used") as taken:
            client.register_campaign(Clone(clone=Remote(url=url)))
        assert taken.value.error == Problem.REFUSED
    assert list((cfg.home / 'repos').iterdir()) == [], 'a retry clones again'


def test_without_a_campaign_named_status_answers_every_campaign(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    with db.session() as s:
        s.add(Campaign(name='other', path='/elsewhere', status=CampaignStatus.STOPPED, config=campaign.config))
        s.commit()
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        listed = client.status(Selection()).campaigns
    assert [c.name for c in listed] == ['demo', 'other'], 'a client that lists campaigns sees them all'
    assert [c.name for c in defaulted(listed)] == ['demo'], 'the CLI shows the only active one'


def test_a_campaign_whose_stored_config_is_invalid_is_listed_as_such_and_the_others_as_ever(
    db: Database,
    campaign: Campaign,
    cfg: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A stored copy that no longer validates, as after an upgrade, is no 500 for every campaign: status and the
    overview list it with what is wrong and how to fix it, and every other campaign in full."""
    with db.session() as s:
        broken = Campaign(name='broken', path='/elsewhere', config=campaign.config)
        s.add(broken)
        s.commit()
        broken_id = broken.id
    store_config(db, broken_id, {})
    with serving_api(cfg), EveryClient.on(cfg.socket_file, System.HUMAN) as client:
        listed = client.status(Selection()).campaigns
        shown = client.overview(Selection()).campaigns
        monkeypatch.setattr(common, 'settings', cfg)
        monkeypatch.setattr(common, 'console', Console(width=200, force_terminal=False))
        cli.app(['status'], result_action='return_value', exit_on_error=False)
        cli.app(['pause', 'broken', '--reason', 'r'], result_action='return_value', exit_on_error=False)
    for entries in (listed, shown):
        ok, invalid = entries
        assert not isinstance(ok, InvalidConfig) and ok.name == 'demo'
        assert isinstance(invalid, InvalidConfig) and (invalid.id, invalid.name) == (broken_id, 'broken')
        assert invalid.fix == '`piloop init /elsewhere`'
        assert invalid.problem.startswith('the stored config is not a valid piloop config'), invalid.problem
    status, paused = capsys.readouterr().out.split('campaign broken paused')
    assert 'demo #1' in status and 'broken #2' in status and 'fix: `piloop init /elsewhere`' in status, status
    assert paused.startswith('; its stored config is invalid, fix it with `piloop init /elsewhere`;'), paused
