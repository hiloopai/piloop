"""The price list capability: what a model's tokens cost at its vendor's list price (``[plugins.price_list]``).

``genai_prices``, built in and the default, reads the prices that the genai-prices package bundles. The ``models`` fact
source asks it for each agent of a campaign, after its own ``prices``.
"""

from abc import ABC, abstractmethod

from piloop.plugins.host import Capability, One, Parameters, Scope, Tier
from piloop.usage import ModelId, Usage


class PriceList(Parameters, ABC):
    """A price list: its parameters, the keys of ``[plugins.price_list]`` besides ``provider``."""

    @abstractmethod
    def usd(self, model: ModelId, usage: Usage) -> float | None:
        """What ``usage``, the tokens of one agent, costs on ``model`` at list price, in USD.

        None: the list has no price for the model. An unknown price is never 0.
        """


PRICE_LISTS = Capability(
    'price_list', PriceList, tier=Tier.PURE, table=One(Scope.CAMPAIGN, default='genai_prices'), noun='price list'
)
"""Every price list by name. ``genai_prices``, built in and the default, is the bundled data of genai-prices."""
