"""``spend``: the built-in fact source of what a campaign spent in USD, the sum of facts that other sources supply.

It supplies ``spend``, which ``hold_at`` and the ``cost`` section read by default, and serves the parts and the sum at
``GET /v1/campaigns/{campaign}/spend`` (``Spend``). An unknown part makes the sum unknown: a part of the spend is never
shown as all of it.
"""

from collections.abc import Iterable
from datetime import datetime

from pydantic import Field
from sqlalchemy.orm import Session

from piloop.api.views import View
from piloop.campaign.stop import gather
from piloop.config import CampaignConfig
from piloop.db.models import Campaign
from piloop.plugins.contribution import Contribution
from piloop.plugins.fact_source.contract import FactSource
from piloop.plugins.fact_source.facts import COMPUTE_USD, KEEPS, MODELS_USD, SPEND, Fact, FactValue, reading


class SpendPart(View):
    """One fact of the spend, in USD."""

    fact: str
    usd: float | None
    """None while its source does not know it: an unknown value is never 0."""
    as_of: datetime | None
    """When its source read it. None: never."""


class Spend(View):
    """What a campaign spent, in USD: the facts that its ``spend`` source names, each a part, and their sum."""

    usd: float | None
    """The sum of the parts. None while one is unknown."""
    parts: list[SpendPart]
    per_keep_usd: float | None
    """``usd`` per keep (``keeps``). None while ``usd`` is unknown, before the first keep, and without a source of
    ``keeps``."""


class SpendFacts(FactSource):
    """``spend``: ``spend``, the sum of ``facts`` in USD, and the route of its parts."""

    facts: list[str] = Field(default=[COMPUTE_USD.name, MODELS_USD.name], min_length=1)
    """The facts in USD that the spend sums, each from another source of ``[plugins.fact_source] use``. The default
    is those of ``leases`` and ``models``."""

    def supplies(self) -> tuple[Fact[FactValue], ...]:
        """``SPEND``."""
        return (SPEND,)

    def reads(self) -> tuple[Fact[float], ...]:
        """Each fact of ``facts``."""
        return tuple(reading(name) for name in self.facts)

    def gather(
        self, s: Session, campaign: Campaign, wanted: frozenset[Fact[FactValue]], now: datetime
    ) -> dict[Fact[FactValue], FactValue]:
        """``SPEND``: the sum of ``facts``, or None while one is unknown."""
        facts = gather(s, campaign, campaign.config, self.reads(), now)
        return {SPEND: total(facts.get(fact) for fact in self.reads())}

    def contribution(self) -> Contribution:
        """The route of the spend, ``GET /v1/campaigns/{campaign}/spend``."""
        from piloop.plugins.fact_source.spend.api import router  # here: the API imports the config, which loads this

        return Contribution(router=router)


def spend_of(s: Session, campaign: Campaign, cfg: CampaignConfig, now: datetime) -> Spend | None:
    """What ``campaign`` spent as of ``now``. None: its ``[plugins.fact_source] use`` has no ``spend`` source."""
    sources = cfg.fact_sources()
    mine = next((source for source in sources if isinstance(source, SpendFacts)), None)
    if mine is None:
        return None
    of = {fact: source for source in sources for fact in source.supplies()}
    facts = gather(s, campaign, cfg, [*mine.reads(), KEEPS], now)
    parts = [
        SpendPart(fact=fact.name, usd=facts.get(fact), as_of=of[fact].as_of(s, campaign, fact, now))
        for fact in mine.reads()
    ]
    usd, keeps = total(part.usd for part in parts), facts.get(KEEPS)
    return Spend(usd=usd, parts=parts, per_keep_usd=None if usd is None or not keeps else usd / keeps)


def total(values: Iterable[float | None]) -> float | None:
    """The sum of ``values``, or None when one is None."""
    usd = 0.0
    for value in values:
        if value is None:
            return None
        usd += value
    return usd
