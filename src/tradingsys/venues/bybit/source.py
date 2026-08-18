"""The Bybit side of the instrument registry.

An :class:`~tradingsys.marketdata.registry.InstrumentSource` over the public REST
client. `venues/bybit/instruments.py` already maps one venue payload to one
:class:`~tradingsys.core.instrument.Instrument` correctly; what did not exist was
anything presenting those mappings as a source, so the registry could not sync the one
venue this system actually records. That gap is recorded in `docs/DECISIONS.md` under
two components being complete without the path between them.

**A perpetual needs two calls, not one.** `instruments-info` carries the contract
definition and `tickers` carries `nextFundingTime`. The funding anchor is required
rather than derived because the interval alone does not say where the cycle starts, and
a wrong phase misprices every overnight hold in a backtest by a full funding payment.
Fetching them separately is not an optimisation to remove: they are different endpoints
and the tickers call is the only published source of the anchor.

**A symbol the venue does not list is a failure, not an omission**, which is the rule
the source protocol states and the reason this raises rather than returning what
resolved. Silently dropping one would be indistinguishable from the venue delisting it,
and the registry would report it as absent and move on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, final

from tradingsys.observability.logging import get_logger
from tradingsys.venues.bybit.instruments import (
    VENUE,
    instrument_from_linear,
    next_funding_time,
)
from tradingsys.venues.errors import InstrumentNotFoundError, VenueResponseError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

    from tradingsys.core.currency import CurrencyRegistry
    from tradingsys.core.instrument import Instrument
    from tradingsys.venues.bybit.instruments import BybitCategory
    from tradingsys.venues.bybit.rest import BybitRestClient

__all__ = ["BybitInstrumentSource"]

logger = get_logger("venues.bybit.source")

LINEAR: Final[BybitCategory] = "linear"
"""The only category this source reads.

Not configurable: `docs/DECISIONS.md` records that the crypto product is linear
perpetuals rather than spot, because the position model and the funding cost that
`SPEC.md` 5.5 constrains holding periods on are properties of perpetuals. A source that
accepted a category would let configuration contradict that decision silently.
"""


@final
class BybitInstrumentSource:
    """Instrument definitions for a configured set of Bybit linear perpetuals."""

    __slots__ = ("_client", "_currencies", "_symbols")

    def __init__(
        self,
        client: BybitRestClient,
        symbols: Sequence[str],
        currencies: CurrencyRegistry,
    ) -> None:
        """
        Args:
            client: Public REST client. Not owned: the caller opens and closes it,
                because one client is shared by everything that talks to this venue and
                its rate limiter is the thing being shared.
            symbols: Venue symbols, as the venue spells them, for example ``ETHUSDT``.
            currencies: Registry used to resolve base, quote and settle assets.
        """
        self._client = client
        self._symbols = tuple(symbols)
        self._currencies = currencies

    @property
    def venue(self) -> str:
        return VENUE

    async def instruments(self) -> Sequence[Instrument]:
        """Fetch a definition for every configured symbol.

        Raises:
            InstrumentNotFoundError: The venue does not list one of the configured
                symbols, or lists it without a funding time.
            VenueResponseError: The venue answered with a payload this client cannot
                interpret, or with a contract that is not a linear perpetual.
            UnknownCurrencyError: The registry does not know one of the assets.
        """
        definitions = await self._client.instruments(LINEAR)
        tickers = await self._client.tickers(LINEAR)

        by_symbol = _index(definitions)
        anchors = _index(tickers)

        instruments: list[Instrument] = []
        for symbol in self._symbols:
            payload = by_symbol.get(symbol)
            if payload is None:
                raise InstrumentNotFoundError(VENUE, symbol)
            ticker = anchors.get(symbol)
            if ticker is None:
                # Without the anchor the funding cycle phase is unknown, and a perpetual
                # priced on a guessed phase is wrong by a whole settlement per overnight
                # hold. Refused rather than defaulted.
                raise VenueResponseError(
                    VENUE,
                    f"{symbol} is listed but has no ticker, so there is no funding "
                    f"anchor. The cycle phase cannot be guessed: a perpetual priced on "
                    f"the wrong phase is wrong by a whole settlement per overnight hold",
                )
            instruments.append(
                instrument_from_linear(
                    payload,
                    self._currencies,
                    funding_anchor=next_funding_time(ticker),
                )
            )

        logger.info(
            "bybit instrument definitions fetched",
            venue=VENUE,
            requested=len(self._symbols),
            resolved=len(instruments),
            catalogue_size=len(by_symbol),
        )
        return instruments


def _index(rows: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Index venue rows by symbol, keeping only rows that carry one.

    A row without a symbol cannot be matched to anything requested, so it is skipped
    here and the requested symbol is reported missing by the caller, which names the
    symbol the operator asked for rather than a payload they did not.
    """
    return {
        str(row["symbol"]): row
        for row in rows
        if isinstance(row.get("symbol"), str) and row["symbol"]
    }
