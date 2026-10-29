"""The admin routes of the API: limits, secrets and tokens."""

from functools import partial
from pathlib import Path

import httpx2 as httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from piloop.actor import System
from piloop.api.client import Client, Refused
from piloop.api.contract import (
    Access,
    LimitBody,
    Located,
    Problem,
    SecretBody,
    TokenBody,
)
from piloop.db.models import (
    Campaign,
    Event,
)
from piloop.db.session import Database
from piloop.net import tokens
from piloop.net.tokens import Tokens
from piloop.plugins.network.https import TIMEOUT
from piloop.settings import Settings
from tests.conftest import serving_api
from tests.net.test_api import token


def test_a_limit_is_set_and_unset_by_an_admin_and_refused_on_a_key_that_is_not_live(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    key = 'executor.pools.serve.slots.evaluation'
    with serving_api(cfg, listen='127.0.0.1:0') as [_, tcp], Client.on(cfg.socket_file, System.HUMAN) as client:
        assert {k.key: k.origin for k in client.limits().keys}[key] == 'default'
        set_ = client.set_limit(key, LimitBody(value=1, reason='the GPU is busy'))
        [limit] = [k for k in set_.keys if k.key == key]
        assert (limit.value, limit.origin, limit.actor, limit.reason) == (1, 'limit', 'human', 'the GPU is busy')
        with pytest.raises(Refused, match='^executor.renew_seconds is not a live key') as not_live:
            client.set_limit('executor.renew_seconds', LimitBody(value=5, reason='r'))
        assert not_live.value.error == Problem.REFUSED
        [config] = [k for k in client.unset_limit(key).keys if k.key == key]
        assert (config.value, config.origin) == (2, 'default'), 'config.toml holds again'
        with pytest.raises(Refused, match=f'^{key} has no limit: see `piloop limits`$') as unknown:
            client.unset_limit(key)
        assert unknown.value.error == Problem.UNKNOWN_LIMIT
        actor = {'Authorization': f'Bearer {token(db, "ana", Access.ACT)}'}
        refused = httpx.get(tcp.url + '/v1/limits', headers=actor, timeout=TIMEOUT)
    assert refused.json() == {'detail': 'ana has act access, and this route needs admin'}


def test_an_admin_sets_lists_and_removes_secrets(
    db: Database, campaign: Campaign, cfg: Settings, tmp_path: Path
) -> None:
    with db.session() as s:
        s.add(Campaign(name='other', path=str(tmp_path / 'other'), config=campaign.config))
        s.commit()
    home, demo, other = Located(), Located(campaign='demo'), Located(campaign='other')
    body = SecretBody(value=SecretStr('s3cret'))
    assert 's3cret' not in repr(body), 'a log of the body shows no value'
    with (
        serving_api(cfg, listen='127.0.0.1:0') as [_, tcp],
        Client.on(cfg.socket_file, System.HUMAN) as admin,
    ):
        assert admin.set_secret('KEY', home, body).campaign is None
        assert admin.set_secret('KEY', demo, body).campaign == 'demo'
        assert admin.set_secret('OWN', other, body).campaign == 'other'
        listed = [(f.name, f.campaign, f.set_by) for f in admin.list_secrets().secrets]
        assert listed == [('KEY', None, 'human'), ('KEY', 'demo', 'human'), ('OWN', 'other', 'human')]
        assert admin.remove_secret('KEY', demo).campaign == 'demo'
        with pytest.raises(Refused, match=r'^campaign demo has no secret KEY: see `piloop secret list`$') as unknown:
            admin.remove_secret('KEY', demo)
        assert unknown.value.error == Problem.UNKNOWN_SECRET
        with pytest.raises(Refused) as nowhere:
            admin.set_secret('KEY', Located(campaign='nowhere'), body)
        assert nowhere.value.error == Problem.UNKNOWN_CAMPAIGN
        with pytest.raises(Refused, match='^name: String should match pattern'):
            admin.set_secret('1X', home, body)
        actor = {'Authorization': f'Bearer {token(db, "ana", Access.ACT)}'}
        refused = httpx.get(tcp.url + '/v1/secrets', headers=actor, timeout=TIMEOUT)
    assert refused.json() == {'detail': 'ana has act access, and this route needs admin'}


def test_act_sets_and_removes_a_campaign_s_secrets_and_only_admin_the_home_s(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    """A person with ``act`` stores a model key for a campaign without ``admin``. The home's secrets stay ``admin``'s."""
    with serving_api(cfg, listen='127.0.0.1:0') as [_, tcp], Client.on(cfg.socket_file, System.HUMAN) as admin:
        bearer = {'Authorization': f'Bearer {token(db, "ana", Access.ACT)}'}
        with Client.connect(httpx.Client(base_url=tcp.url, headers=bearer)) as ana:
            body = SecretBody(value=SecretStr('hil_key'))
            assert ana.set_secret('REVOLVER_TOKEN', Located(campaign='demo'), body).campaign == 'demo'
            assert ana.remove_secret('REVOLVER_TOKEN', Located(campaign='demo')).campaign == 'demo'
            for write in (
                partial(ana.set_secret, 'KEY', Located(), body),
                partial(ana.remove_secret, 'KEY', Located()),
            ):
                with pytest.raises(Refused, match=r"^ana has act access, and the home's secrets need admin$"):
                    write()
        admin.set_secret('REVOLVER_TOKEN', Located(campaign='demo'), body)
        assert [(f.name, f.campaign, f.set_by) for f in admin.list_secrets().secrets] == [
            ('REVOLVER_TOKEN', 'demo', 'human')
        ], 'the home has no secret: the refused writes changed nothing'
    with db.session() as s:
        set_by = s.scalars(select(Event.actor).where(Event.kind == 'secret.set').order_by(Event.id)).all()
    assert set_by[0] == 'human:ana'


def test_an_admin_makes_a_token_whose_secret_only_its_answer_carries_and_revokes_it(
    db: Database, campaign: Campaign, cfg: Settings
) -> None:
    with serving_api(cfg, listen='127.0.0.1:0') as [_, tcp], Client.on(cfg.socket_file, System.HUMAN) as client:
        made = client.create_token(TokenBody(name='gpu1', access=Access.WORKER))
        secret = made.secret.get_secret_value()
        assert secret not in repr(made), 'a log of the answer shows no secret'
        assert Tokens(db).verify(secret) == tokens.Caller('gpu1', Access.WORKER)
        with pytest.raises(Refused, match="^a token is called 'gpu1' already") as taken:
            client.create_token(TokenBody(name='gpu1', access=Access.READ))
        assert taken.value.error == Problem.REFUSED
        [listed] = client.list_tokens().tokens
        assert (listed.name, listed.access, listed.created_at) == ('gpu1', Access.WORKER, made.created_at)
        assert client.revoke_token('gpu1').tokens == []
        with pytest.raises(Refused, match="^no token is called 'gpu1'$") as unknown:
            client.revoke_token('gpu1')
        assert unknown.value.error == Problem.UNKNOWN_TOKEN
        actor = {'Authorization': f'Bearer {token(db, "ana", Access.ACT)}'}
        refused = httpx.post(
            tcp.url + '/v1/tokens', headers=actor, json={'name': 'mine', 'access': 'admin'}, timeout=TIMEOUT
        )
    assert refused.json() == {'detail': 'ana has act access, and this route needs admin'}
