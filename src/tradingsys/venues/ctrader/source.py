"""The cTrader side of the instrument registry.

An :class:`~tradingsys.marketdata.registry.InstrumentSource` over an authenticated
connection. It fetches the account's catalogue once and then the full metadata for each
configured symbol, because a broker publishes close to two thousand symbols and this
system trades four.

**A symbol the venue does not list is a failure, not an omission.** Returning the
instruments that resolved and quietly dropping the rest would look identical to the
venue having delisted one, and the registry sync would report it as absent and move on.
So a missing symbol raises and the refresh does not land.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, final

from tradingsys.observability.logging import get_logger
from tradingsys.venues.ctrader.framing import VENUE
from tradingsys.venues.ctrader.symbols import fetch_catalogue, fetch_instrument

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.currency import CurrencyRegistry
    from tradingsys.core.instrument import Instrument
    from tradingsys.venues.ctrader.connection import CTraderConnection

__all__ = ["CTraderInstrumentSource"]

logger = get_logger("venues.ctrader.source")


@final
class CTraderInstrumentSource:
    """Instrument definitions for a configured set of cTrader symbols."""

    __slots__ = ("_connection", "_currencies", "_symbols")

    def __init__(
        self,
        connection: CTraderConnection,
        symbols: Sequence[str],
        currencies: CurrencyRegistry,
    ) -> None:
        self._connection = connection
        self._symbols = tuple(symbols)
        self._currencies = currencies

    @property
    def venue(self) -> str:
        return VENUE

    async def instruments(self) -> Sequence[Instrument]:
        """Fetch metadata for every configured symbol.

        Raises:
            InstrumentNotFoundError: The venue does not list one of the configured
                symbols on this account.
            VenueResponseError: The venue answered with metadata this client cannot
                interpret, or with a different symbol than the one requested.
        """
        catalogue = await fetch_catalogue(self._connection)
        definitions = [
            await fetch_instrument(self._connection, catalogue, symbol, self._currencies)
            for symbol in self._symbols
        ]
        logger.info(
            "ctrader instrument definitions fetched",
            venue=VENUE,
            requested=len(self._symbols),
            resolved=len(definitions),
            catalogue_size=len(catalogue.symbols),
        )
        return definitions
