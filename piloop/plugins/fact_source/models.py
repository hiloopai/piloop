"""``models``: the built-in fact source of what the campaign's agents spent on models, from the tokens piloop counts.

The price is the API-equivalent cost: the tokens of each agent that ended (``agent_sessions.usage``) at the list price
of its model, from ``prices`` else the campaign's price list (``[plugins.price_list]``). An agent on a subscription is
billed otherwise, so this is not an invoice.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from piloop.config import CampaignConfig
from piloop.db.models import AgentSession, Campaign
from piloop.plugins.fact_source.contract import FactSource
from piloop.plugins.fact_source.facts import MODELS_USD, Fact, FactValue
from piloop.plugins.harness.contract import ModelId, Usage
from piloop.plugins.price_list.contract import PRICE_LISTS

PER = 1_000_000
"""A price is for this many tokens."""


class Price(BaseModel):
    """What a million tokens of each kind cost on one model, in USD."""

    model_config = ConfigDict(extra='forbid', frozen=True, use_attribute_docstrings=True)

    input: float = Field(ge=0)
    """The input that the prompt cache did not serve."""
    output: float = Field(ge=0)
    """The output."""
    cache_read: float = Field(ge=0)
    """The input that the prompt cache served."""
    cache_write: float = Field(ge=0)
    """The input written to the prompt cache."""

    def usd(self, usage: Usage) -> float:
        """What ``usage`` costs at this price."""
        tokens = (usage.input, usage.output, usage.cache_read, usage.cache_write)
        prices = (self.input, self.output, self.cache_read, self.cache_write)
        return sum(n * price for n, price in zip(tokens, prices, strict=True)) / PER


class ModelFacts(FactSource):
    """``models``: ``models_usd``, the tokens of the campaign's agents that ended, at their models' prices."""

    prices: dict[str, Price] = {}
    """The price of a model, by its name in ``agent_sessions.model``, over the campaign's price list. Example: ``{
    "claude-sonnet-4-5" = { input = 3.0, output = 15.0, cache_read = 0.3, cache_write = 3.75 } }``."""

    def supplies(self) -> tuple[Fact[FactValue], ...]:
        """``MODELS_USD``."""
        return (MODELS_USD,)

    def gather(
        self, s: Session, campaign: Campaign, wanted: frozenset[Fact[FactValue]], now: datetime
    ) -> dict[Fact[FactValue], FactValue]:
        """``MODELS_USD``: None when an agent that ended ran a model without a price."""
        return {MODELS_USD: self.models_usd(s, campaign)}

    def models_usd(self, s: Session, campaign: Campaign) -> float | None:
        """The tokens of the campaign's agents that ended, each at its model's price. None: a model has no price."""
        cfg = campaign.config
        ended = select(AgentSession.harness, AgentSession.model, AgentSession.usage).where(
            AgentSession.campaign_id == campaign.id, AgentSession.usage.is_not(None)
        )
        usd = 0.0
        for harness, model, usage in s.execute(ended):
            if (cost := self.usd(cfg, harness, model, usage)) is None:
                return None
            usd += cost
        return usd

    def usd(self, cfg: CampaignConfig, harness: str | None, model: str | None, usage: Usage | None) -> float | None:
        """What ``usage`` of ``model`` on the campaign's ``harness`` costs: from ``prices``, else the price list."""
        match model, usage:
            case str(), Usage():
                if (price := self.prices.get(model)) is not None:
                    return price.usd(usage)
                found = cfg.harness.get(harness or '')
                named = ModelId(None, model) if found is None else found.model_id(model)
                return PRICE_LISTS.selected(cfg.plugins).usd(named, usage)
            case _:
                return None  # the harness's own default model, which piloop does not know
