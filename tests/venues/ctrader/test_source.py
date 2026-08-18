"""The cTrader instrument source: the other edge that was written and unreached.

`venues/ctrader/source.py` measured 0 percent coverage while `PROGRESS.md` recorded the
registry's cTrader source as built. The mapping in `instruments.py` was tested and the
transport was tested; nothing exercised the thing that joins them to the registry.

The refusals are the point. A source that returns the symbols that resolved is
indistinguishable from a venue that delisted the rest, and the registry would write that
absence into the instrument table as though the venue had said it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.venues.ctrader.scripted_venue import ScriptedAccount, ScriptedVenue
from tests.venues.ctrader.test_instruments import full_symbol, light_symbol
from tradingsys.config.settings import ForexVenueSettings, VenueEnvironment
from tradingsys.core.currency import default_registry
from tradingsys.venues.ctrader.connection import CTraderConnection
from tradingsys.venues.ctrader.source import CTraderInstrumentSource
from tradingsys.venues.errors import InstrumentNotFoundError, VenueResponseError

if TYPE_CHECKING:
    from collections.abc import Sequence

pytestmark = pytest.mark.asyncio

ACCOUNT = ScriptedAccount(ctid_trader_account_id=42_000_001, trader_login=5_325_402, is_live=False)

EUR = 10
USD = 20


def settings() -> ForexVenueSettings:
    return ForexVenueSettings(
        enabled=True,
        environment=VenueEnvironment.PRACTICE,
        demo_api_host="demo.ctraderapi.com",
        live_api_host="live.ctraderapi.com",
        api_port=5035,
        token_url="https://openapi.ctrader.com/apps/token",
        request_timeout_seconds=5.0,
        stream_read_timeout_seconds=95.0,
        max_retries=3,
        retry_backoff_seconds=0.5,
        max_requests_per_second=30.0,
        heartbeat_interval_seconds=10.0,
        token_refresh_margin_seconds=259200.0,
        account_id="5325402",
        client_id="client",
        client_secret="secret",
        access_token="token",
        refresh_token="refresh",
    )


def venue_with(*names: str, definitions: bool = True) -> ScriptedVenue:
    """A venue listing each name, with a full definition unless suppressed."""
    lights = [light_symbol(name=name, symbol_id=index + 1) for index, name in enumerate(names)]
    for light in lights:
        light.baseAssetId = EUR
        light.quoteAssetId = USD
    full = (
        {index + 1: full_symbol(symbol_id=index + 1) for index, _ in enumerate(names)}
        if definitions
        else {}
    )
    return ScriptedVenue(
        accounts=[ACCOUNT],
        assets={EUR: "EUR", USD: "USD"},
        light_symbols=lights,
        full_symbols=full,
    )


async def resolve(venue: ScriptedVenue, symbols: Sequence[str]) -> object:
    connection = CTraderConnection(venue, settings())
    await connection.open()
    try:
        return await CTraderInstrumentSource(connection, symbols, default_registry).instruments()
    finally:
        await connection.close()


class TestItResolvesTheConfiguredUniverse:
    async def test_a_definition_comes_back_for_each_symbol(self) -> None:
        instruments = await resolve(venue_with("EURUSD", "GBPUSD"), ["EURUSD", "GBPUSD"])

        assert [i.venue_symbol for i in instruments] == ["EURUSD", "GBPUSD"]  # type: ignore[attr-defined]

    async def test_only_the_configured_symbols_are_fetched(self) -> None:
        """The broker publishes 1939 symbols and this system trades four, so the source
        must not pull the catalogue's full metadata."""
        instruments = await resolve(venue_with("EURUSD", "GBPUSD", "USDJPY"), ["EURUSD"])

        assert len(instruments) == 1  # type: ignore[arg-type]

    async def test_the_venue_name_is_reported(self) -> None:
        connection = CTraderConnection(venue_with("EURUSD"), settings())
        source = CTraderInstrumentSource(connection, ["EURUSD"], default_registry)

        assert source.venue == "ctrader"


class TestItRefusesRatherThanReturningLess:
    async def test_a_symbol_the_venue_does_not_list_raises(self) -> None:
        """Returning what resolved would look exactly like a delisting, and the registry
        would record the absence as though the venue had reported it."""
        with pytest.raises(InstrumentNotFoundError, match="GBPUSD"):
            await resolve(venue_with("EURUSD"), ["EURUSD", "GBPUSD"])

    async def test_a_listed_symbol_with_no_definition_raises(self) -> None:
        """The venue answers the by-id lookup with what it has, so a symbol it cannot
        describe is simply absent from the reply rather than reported as an error."""
        with pytest.raises(VenueResponseError):
            await resolve(venue_with("EURUSD", definitions=False), ["EURUSD"])

    async def test_nothing_lands_when_one_symbol_fails(self) -> None:
        """A partial refresh is worse than a failed one: the registry would report the
        missing instrument as drift rather than as a fetch that did not complete."""
        with pytest.raises(InstrumentNotFoundError):
            await resolve(venue_with("EURUSD", "GBPUSD"), ["EURUSD", "ABSENT"])
