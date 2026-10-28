"""Model prices: each harness names its model as the vendor's API does, and the ``genai_prices`` list prices it.

The list is the data that the genai-prices package bundles: it prices with no network. A model that it does not have
has no price, never 0.
"""

import socket

import pytest

from piloop.plugins.harness.claude import Claude
from piloop.plugins.harness.codex import Codex
from piloop.plugins.harness.contract import Custom
from piloop.plugins.harness.pi import Pi
from piloop.plugins.price_list.contract import PRICE_LISTS
from piloop.plugins.price_list.genai_prices import GenaiPrices
from piloop.usage import ModelId, Usage

M = 1_000_000


def tokens(input_: int = 0, output: int = 0, cache_read: int = 0, cache_write: int = 0, turns: int = 10) -> Usage:
    """Ten turns by default: each under the 200k tokens above which a request costs more."""
    return Usage(input=input_, output=output, cache_read=cache_read, cache_write=cache_write, total=0, turns=turns)


def test_claude_names_its_models_at_anthropic() -> None:
    assert Claude().model_id('claude-sonnet-4-5') == ModelId('anthropic', 'claude-sonnet-4-5')


def test_codex_names_its_models_at_openai() -> None:
    assert Codex().model_id('gpt-5-codex') == ModelId('openai', 'gpt-5-codex')


def test_pi_names_the_provider_of_its_model_and_drops_a_thinking_level() -> None:
    pi = Pi()
    assert pi.model_id('anthropic/claude-sonnet-4-5') == ModelId('anthropic', 'claude-sonnet-4-5')
    assert pi.model_id('openrouter/anthropic/claude-sonnet-4.5:high') == ModelId(
        'openrouter', 'anthropic/claude-sonnet-4.5'
    )
    assert pi.model_id('gpt-5:low') == ModelId(None, 'gpt-5'), 'a bare id names no vendor'
    assert pi.model_id('vendor/model:v2') == ModelId('vendor', 'model:v2'), 'a colon that is no thinking level stays'


def test_a_harness_of_its_own_names_the_model_alone() -> None:
    custom = Custom.model_validate({'executable': 'my-agent', 'headless': ['{prompt}']})
    assert custom.model_id('claude-haiku-4-5') == ModelId(None, 'claude-haiku-4-5')


def test_genai_prices_is_the_default_list_and_prices_each_kind_of_token_at_the_vendor_s_list_price() -> None:
    listed = PRICE_LISTS.selected({})
    assert isinstance(listed, GenaiPrices)
    sonnet = ModelId('anthropic', 'claude-sonnet-4-5')
    assert listed.usd(sonnet, tokens(input_=M)) == pytest.approx(3.0)
    assert listed.usd(sonnet, tokens(output=M)) == pytest.approx(15.0)
    assert listed.usd(sonnet, tokens(cache_read=M)) == pytest.approx(0.3)
    assert listed.usd(sonnet, tokens(cache_write=M)) == pytest.approx(3.75)
    assert listed.usd(ModelId(None, 'claude-sonnet-4-5'), tokens(input_=M)) == pytest.approx(3.0), 'the name says it'
    assert listed.usd(ModelId('openai', 'gpt-5'), tokens(input_=M, output=M)) == pytest.approx(11.25)


def test_a_price_by_the_size_of_a_request_is_that_of_the_mean_turn() -> None:
    """Above 200k tokens of input, a request to Sonnet costs twice as much: 300k in one turn does, in two turns not."""
    sonnet = ModelId('anthropic', 'claude-sonnet-4-5')
    assert GenaiPrices().usd(sonnet, tokens(input_=300_000, turns=1)) == pytest.approx(1.8)
    assert GenaiPrices().usd(sonnet, tokens(input_=300_000, turns=2)) == pytest.approx(0.9)
    assert GenaiPrices().usd(sonnet, tokens(input_=300_000, turns=0)) == pytest.approx(1.8), 'no turns: one'


def test_a_model_or_vendor_that_the_list_lacks_has_no_price() -> None:
    assert GenaiPrices().usd(ModelId('anthropic', 'sonnet'), tokens(input_=M)) is None, 'an alias is no model id'
    assert GenaiPrices().usd(ModelId(None, 'my-model'), tokens(input_=M)) is None
    assert GenaiPrices().usd(ModelId('nowhere', 'gpt-5'), tokens(input_=M)) is None


def test_the_list_prices_with_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def offline(*_args: object, **_kwargs: object) -> None:
        raise OSError('no network')

    monkeypatch.setattr(socket.socket, 'connect', offline)
    monkeypatch.setattr(socket, 'getaddrinfo', offline)
    assert GenaiPrices().usd(ModelId('anthropic', 'claude-haiku-4-5'), tokens(input_=M)) == pytest.approx(1.0)
