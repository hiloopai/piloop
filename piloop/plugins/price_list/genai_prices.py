"""``genai_prices``: the built-in price list, the data that pydantic's genai-prices package bundles.

It reads no network: the prices are those of the installed package. A newer package has newer prices.
"""

from decimal import Decimal

from genai_prices import Usage as Tokens, calc_price

from piloop.plugins.price_list.contract import PriceList
from piloop.usage import ModelId, Usage


class GenaiPrices(PriceList):
    """``genai_prices``: the list prices of https://github.com/pydantic/genai-prices, by vendor and model id.

    A price that changes with the size of a request, such as a long context, is that of the agent's mean request: its
    tokens over its turns.
    """

    def usd(self, model: ModelId, usage: Usage) -> float | None:
        """The price of each of the agent's turns, as the mean turn, times its turns. None: no such vendor or model."""
        turns = max(usage.turns, 1)
        mean = Tokens(
            input_tokens=Decimal(usage.input + usage.cache_read + usage.cache_write) / turns,
            output_tokens=Decimal(usage.output) / turns,
            cache_read_tokens=Decimal(usage.cache_read) / turns,
            cache_write_tokens=Decimal(usage.cache_write) / turns,
        )
        try:
            price = calc_price(mean, model.name, provider_id=model.vendor)
        except LookupError:
            return None  # the data has no such vendor or model: the price is unknown
        return float(price.total_price * turns)
