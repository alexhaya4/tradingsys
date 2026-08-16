"""Fetching symbol metadata over an authenticated connection.

Three requests, in a fixed order, because each depends on the one before it.

The asset list turns the numeric asset ids that symbols carry into names such as EUR
and USD. The symbol list gives the venue's name for each symbol and its base and quote
asset ids, but not the precision or the volume limits. Only the symbol by id request
carries those, and it is asked for explicitly rather than for the whole book, because
a broker publishes thousands of symbols and this system trades four.

Everything here refuses rather than defaults. A symbol the venue does not list, an
asset id it does not name, or a metadata field it omits all raise, because the
alternative is an instrument definition with a plausible looking number in it that
nothing downstream can tell apart from a real one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from tradingsys.observability.logging import get_logger
from tradingsys.venues.ctrader.framing import VENUE
from tradingsys.venues.ctrader.instruments import instrument_from_symbol
from tradingsys.venues.ctrader.messages.OpenApiMessages_pb2 import (
    ProtoOAAssetListReq,
    ProtoOAAssetListRes,
    ProtoOASymbolByIdReq,
    ProtoOASymbolByIdRes,
    ProtoOASymbolsListReq,
    ProtoOASymbolsListRes,
)
from tradingsys.venues.errors import InstrumentNotFoundError, VenueResponseError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tradingsys.core.currency import CurrencyRegistry
    from tradingsys.core.instrument import Instrument
    from tradingsys.venues.ctrader.connection import CTraderConnection
    from tradingsys.venues.ctrader.messages.OpenApiModelMessages_pb2 import ProtoOALightSymbol

__all__ = ["SymbolCatalogue", "fetch_catalogue"]

logger = get_logger("venues.ctrader.symbols")

ASSET_LIST_REQ: Final = 2112
ASSET_LIST_RES: Final = 2113
SYMBOLS_LIST_REQ: Final = 2114
SYMBOLS_LIST_RES: Final = 2115
SYMBOL_BY_ID_REQ: Final = 2116
SYMBOL_BY_ID_RES: Final = 2117


class SymbolCatalogue:
    """What the venue publishes about the symbols on one account.

    Holds the lightweight records and the asset names, which are cheap to fetch once
    and are what every later lookup is resolved through.
    """

    __slots__ = ("_assets", "_by_name", "_symbols")

    def __init__(self, symbols: Sequence[ProtoOALightSymbol], assets: dict[int, str]) -> None:
        self._symbols = tuple(symbols)
        self._assets = assets
        self._by_name = {symbol.symbolName: symbol for symbol in self._symbols}

    @property
    def symbols(self) -> tuple[ProtoOALightSymbol, ...]:
        return self._symbols

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_name))

    def light(self, name: str) -> ProtoOALightSymbol:
        """The lightweight record for a venue symbol name.

        Raises:
            InstrumentNotFoundError: The venue does not list that name on this account.
        """
        try:
            return self._by_name[name]
        except KeyError:
            raise InstrumentNotFoundError(VENUE, name) from None

    def asset_name(self, asset_id: int, *, symbol_name: str) -> str:
        """The name of an asset id.

        Raises:
            VenueResponseError: The asset list did not name that id, so the currency
                of the instrument cannot be established.
        """
        try:
            return self._assets[asset_id]
        except KeyError:
            raise VenueResponseError(
                VENUE,
                f"symbol {symbol_name} references asset id {asset_id}, which the venue's "
                f"own asset list does not name. The instrument's currency cannot be "
                f"established, and guessing it would misprice every position in it.",
            ) from None


async def fetch_catalogue(connection: CTraderConnection) -> SymbolCatalogue:
    """Fetch the asset list and the symbol list for the authenticated account."""
    account_id = connection.ctid_trader_account_id

    asset_request = ProtoOAAssetListReq()
    asset_request.ctidTraderAccountId = account_id
    assets = await connection.request(
        ASSET_LIST_REQ, asset_request, ProtoOAAssetListRes(), ASSET_LIST_RES
    )
    if not isinstance(assets, ProtoOAAssetListRes):  # pragma: no cover - defensive
        raise VenueResponseError(VENUE, "the asset list returned the wrong type")

    by_id: dict[int, str] = {}
    for asset in assets.asset:
        if not asset.HasField("name"):
            raise VenueResponseError(
                VENUE, f"asset id {asset.assetId} was published without a name"
            )
        by_id[asset.assetId] = asset.name

    symbol_request = ProtoOASymbolsListReq()
    symbol_request.ctidTraderAccountId = account_id
    symbols = await connection.request(
        SYMBOLS_LIST_REQ, symbol_request, ProtoOASymbolsListRes(), SYMBOLS_LIST_RES
    )
    if not isinstance(symbols, ProtoOASymbolsListRes):  # pragma: no cover - defensive
        raise VenueResponseError(VENUE, "the symbol list returned the wrong type")

    logger.info(
        "ctrader catalogue fetched",
        venue=VENUE,
        symbols=len(symbols.symbol),
        assets=len(by_id),
    )
    return SymbolCatalogue(symbols.symbol, by_id)


async def fetch_instrument(
    connection: CTraderConnection,
    catalogue: SymbolCatalogue,
    venue_symbol: str,
    currencies: CurrencyRegistry,
) -> Instrument:
    """Fetch the full metadata for one symbol and map it to an instrument.

    Raises:
        InstrumentNotFoundError: The venue does not list that symbol on this account.
        VenueResponseError: The venue answered without the symbol it was asked for, or
            with metadata this client cannot interpret.
    """
    light = catalogue.light(venue_symbol)

    request = ProtoOASymbolByIdReq()
    request.ctidTraderAccountId = connection.ctid_trader_account_id
    request.symbolId.append(light.symbolId)
    response = await connection.request(
        SYMBOL_BY_ID_REQ, request, ProtoOASymbolByIdRes(), SYMBOL_BY_ID_RES
    )
    if not isinstance(response, ProtoOASymbolByIdRes):  # pragma: no cover - defensive
        raise VenueResponseError(VENUE, "the symbol request returned the wrong type")

    matching = [symbol for symbol in response.symbol if symbol.symbolId == light.symbolId]
    if not matching:
        raise VenueResponseError(
            VENUE,
            f"asked for symbol {venue_symbol} (id {light.symbolId}) and the venue "
            f"answered with {[s.symbolId for s in response.symbol]}. Mapping a "
            f"different symbol's precision onto this one would misprice it.",
        )

    return instrument_from_symbol(
        matching[0],
        light,
        currencies,
        base_asset=catalogue.asset_name(light.baseAssetId, symbol_name=venue_symbol),
        quote_asset=catalogue.asset_name(light.quoteAssetId, symbol_name=venue_symbol),
    )
