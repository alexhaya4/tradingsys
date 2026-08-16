"""Typed application settings.

Configuration is resolved from four layers, later layers winning:

1. Defaults declared on the models below.
2. ``base.toml`` in the configuration directory: non-secret values shared everywhere.
3. ``{environment}.toml``: non-secret per-environment overrides.
4. The secrets directory and environment variables: credentials, and nothing else.

Every model forbids unknown keys, so a typo in a config file aborts startup instead of
being silently ignored. Every value is typed and range checked. Nothing in this module
reads a value lazily: if the process is running, its configuration was valid.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final, Self, final
from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from tradingsys.config.sources import (
    MappingEnvSource,
    TomlLayeredSource,
    read_secrets_directory,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "AppSettings",
    "CryptoVenueSettings",
    "DatabaseSettings",
    "Environment",
    "ForexVenueSettings",
    "LogFormat",
    "LogLevel",
    "OAuthCredentials",
    "ObservabilitySettings",
    "RedisSettings",
    "Settings",
    "SettingsContext",
    "VenueEnvironment",
    "VenuesSettings",
    "settings_context",
]

ENV_PREFIX: Final = "TRADINGSYS_"
ENV_NESTED_DELIMITER: Final = "__"
DEFAULT_CONFIG_DIRNAME: Final = "config"
REDACTED: Final = "REDACTED"
"""Placeholder substituted for a secret in any string meant for logs."""

Port = Annotated[int, Field(ge=1, le=65_535)]
PositiveSeconds = Annotated[float, Field(gt=0)]
NonEmptyStr = Annotated[str, Field(min_length=1)]


class Environment(StrEnum):
    """Deployment environment, which selects the per-environment config file."""

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class VenueEnvironment(StrEnum):
    """Whether a venue connection points at simulated or real money.

    The two are separate accounts behind identical API shapes, which is exactly why
    the distinction has to be explicit in configuration and checked at startup.
    """

    PRACTICE = "practice"
    LIVE = "live"


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LogFormat(StrEnum):
    JSON = "json"
    CONSOLE = "console"


class _Section(BaseModel):
    """Base for configuration sections: strict, immutable, no surprises."""

    model_config = {"extra": "forbid", "frozen": True}


@final
class AppSettings(_Section):
    """Identity and lifecycle of the process itself."""

    name: NonEmptyStr
    environment: Environment
    shutdown_grace_seconds: PositiveSeconds
    allow_live_trading: bool = False
    """Must be true before any venue may be configured as live.

    A second, deliberate switch beyond the venue's own environment setting, so that
    copying a production config into a developer environment cannot arm real orders.
    """


@final
class DatabaseSettings(_Section):
    """PostgreSQL and TimescaleDB connection settings."""

    host: NonEmptyStr
    port: Port
    database: NonEmptyStr
    user: NonEmptyStr
    password: SecretStr
    min_pool_size: Annotated[int, Field(ge=0)]
    max_pool_size: Annotated[int, Field(ge=1)]
    connect_timeout_seconds: PositiveSeconds
    command_timeout_seconds: PositiveSeconds
    statement_cache_size: Annotated[int, Field(ge=0)]
    ssl_mode: NonEmptyStr

    @model_validator(mode="after")
    def _check_pool_bounds(self) -> Self:
        if self.min_pool_size > self.max_pool_size:
            raise ValueError(
                f"min_pool_size {self.min_pool_size} exceeds max_pool_size {self.max_pool_size}"
            )
        return self

    def dsn(self, *, reveal_password: bool = False) -> str:
        """PostgreSQL URI for this database.

        Args:
            reveal_password: When false, the password is replaced by a placeholder so
                the result is safe to log. Only the connection code passes true.
        """
        password = quote(self.password.get_secret_value(), safe="") if reveal_password else REDACTED
        return (
            f"postgresql://{quote(self.user, safe='')}:{password}"
            f"@{self.host}:{self.port}/{quote(self.database, safe='')}"
        )

    def __str__(self) -> str:
        return self.dsn()


@final
class RedisSettings(_Section):
    """Redis connection settings, used for coordination and caching."""

    host: NonEmptyStr
    port: Port
    db: Annotated[int, Field(ge=0)]
    username: str | None = None
    password: SecretStr | None = None
    socket_timeout_seconds: PositiveSeconds = 5.0
    connect_timeout_seconds: PositiveSeconds = 5.0

    def url(self, *, reveal_password: bool = False) -> str:
        """Redis URL. The password is masked unless explicitly revealed."""
        credentials = ""
        if self.password is not None:
            secret = (
                quote(self.password.get_secret_value(), safe="") if reveal_password else REDACTED
            )
            credentials = f"{quote(self.username or '', safe='')}:{secret}@"
        elif self.username:
            credentials = f"{quote(self.username, safe='')}@"
        return f"redis://{credentials}{self.host}:{self.port}/{self.db}"

    def __str__(self) -> str:
        return self.url()


@final
class ObservabilitySettings(_Section):
    """Logging, metrics, and the operational HTTP endpoints."""

    service_name: NonEmptyStr
    log_level: LogLevel
    log_format: LogFormat
    http_host: NonEmptyStr
    http_port: Port
    health_path: NonEmptyStr
    ready_path: NonEmptyStr
    metrics_path: NonEmptyStr
    readiness_timeout_seconds: PositiveSeconds

    @model_validator(mode="after")
    def _check_paths(self) -> Self:
        paths = {
            "health_path": self.health_path,
            "ready_path": self.ready_path,
            "metrics_path": self.metrics_path,
        }
        for name, value in paths.items():
            if not value.startswith("/"):
                raise ValueError(f"{name} must start with '/', got {value!r}")
        if len(set(paths.values())) != len(paths):
            raise ValueError(f"health, ready, and metrics paths must differ, got {paths}")
        return self


@final
class OAuthCredentials(BaseModel):
    """An OAuth2 credential set whose access token expires and must be refreshed.

    Assembled by :meth:`ForexVenueSettings.require_credentials` from four separately
    supplied environment variables rather than parsed as a nested config section: the
    four are kept flat so that a validation failure can name exactly which one is
    missing, which "credentials is required" cannot.

    This is the shape cTrader Open API uses, and it is a different animal from a static
    API key. Two properties of it drive the design:

    *The access token expires*, typically after about thirty days, so a process that
    reads its credentials only at startup authenticates perfectly for a month and then
    fails. Expiry is therefore something the venue interface exposes and a supervisor
    acts on, not an implementation detail buried in an adapter.

    *Refreshing can rotate the refresh token itself.* When it does, the new one must be
    persisted before the process next restarts, or the account is locked out until
    someone re-authorises by hand. The refresh call returns the new material rather
    than swallowing it, so the caller is forced to decide where it goes.

    All four values are secrets. ``client_id`` counts as one here even though it is
    sometimes treated as public, because together with the secret it mints tokens.
    """

    model_config = {"extra": "forbid", "frozen": True}

    client_id: SecretStr
    client_secret: SecretStr
    access_token: SecretStr
    refresh_token: SecretStr


@final
class ForexVenueSettings(_Section):
    """Connection settings for the forex venue.

    Currently cTrader Open API, reached through a broker. The credential shape and the
    endpoints below are that venue's; the abstractions in :mod:`tradingsys.venues` are
    not, and must stay that way. Configuration is the correct place for a venue to be
    named, because a deployment connects to one specific venue, whereas the interfaces
    have to accommodate all of them.

    Endpoints are configuration rather than constants because demo and live differ only
    by hostname, and pinning them in code would make that distinction invisible in
    review.

    The trading connection is a persistent TLS socket rather than an HTTP endpoint,
    which is why it is a host and port rather than a URL. ``token_url`` is separate and
    is HTTPS, because OAuth token exchange is an ordinary HTTP call.
    """

    enabled: bool
    environment: VenueEnvironment
    demo_api_host: NonEmptyStr
    live_api_host: NonEmptyStr
    api_port: Port
    token_url: NonEmptyStr
    request_timeout_seconds: PositiveSeconds
    stream_read_timeout_seconds: PositiveSeconds
    max_retries: Annotated[int, Field(ge=0)]
    retry_backoff_seconds: PositiveSeconds
    max_requests_per_second: Annotated[float, Field(gt=0)]
    token_refresh_margin_seconds: PositiveSeconds
    """How long before expiry a refresh is attempted.

    Refreshing exactly at expiry means any transient failure at that moment locks the
    account out until someone re-authorises by hand. The margin buys retries, and is
    configuration because it trades refresh traffic against that risk.
    """
    account_id: NonEmptyStr | None = None
    """The trading account number. Deployment specific but not a secret, so it may
    appear in a per-environment config file as well as in the environment.

    Optional only so that the venue can be described while disabled; enabling it
    without an account is rejected below.
    """
    client_id: SecretStr | None = None
    client_secret: SecretStr | None = None
    access_token: SecretStr | None = None
    refresh_token: SecretStr | None = None

    @model_validator(mode="after")
    def _check_token_url(self) -> Self:
        if not self.token_url.startswith("https://"):
            raise ValueError(
                f"token_url must be an https URL; a client secret must never cross the "
                f"network in clear text, got {self.token_url!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_the_two_hosts_differ(self) -> Self:
        """The demo and live hosts must not be the same string.

        If they were, :attr:`api_host` would return the same endpoint for both
        environments and the separation below would be decorative.
        """
        if self.demo_api_host == self.live_api_host:
            raise ValueError(
                f"demo_api_host and live_api_host are both {self.demo_api_host!r}. The two "
                f"environments are fully separated at the venue: one cannot serve the "
                f"other's accounts, so they cannot share a hostname."
            )
        return self

    @property
    def api_host(self) -> str:
        """The endpoint for the configured environment.

        Derived rather than configured. Both hostnames are held in configuration so
        they stay reviewable and correctable, but which one is used is a function of
        :attr:`environment`, so a deployment cannot point a demo account at the live
        endpoint or the reverse: there is no field in which to express the mismatch.

        The venue enforces the same separation from its side, a live endpoint refuses
        demo accounts and vice versa, so the only thing a mismatch could produce is a
        confusing authentication failure. Making it unrepresentable is cheaper than
        diagnosing it.
        """
        if self.environment is VenueEnvironment.LIVE:
            return self.live_api_host
        return self.demo_api_host

    @model_validator(mode="after")
    def _check_credentials_when_enabled(self) -> Self:
        """An enabled venue must be able to authenticate.

        Every absent field is named, because supplying four separate variables one at a
        time and being told only "credentials are required" each time is a miserable
        way to spend an afternoon.
        """
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("account_id", self.account_id),
                ("client_id", self.client_id),
                ("client_secret", self.client_secret),
                ("access_token", self.access_token),
                ("refresh_token", self.refresh_token),
            )
            if value is None
        ]
        if missing:
            listed = ", ".join(missing)
            variables = ", ".join(
                f"{ENV_PREFIX}VENUES{ENV_NESTED_DELIMITER}FOREX{ENV_NESTED_DELIMITER}{name.upper()}"
                for name in missing
            )
            raise ValueError(
                f"the forex venue is enabled but {listed} "
                f"{'is' if len(missing) == 1 else 'are'} not set. Supply them through the "
                f"environment ({variables}), never through a config file."
            )
        return self

    @property
    def has_credentials(self) -> bool:
        """Whether the venue can authenticate."""
        return None not in (
            self.account_id,
            self.client_id,
            self.client_secret,
            self.access_token,
            self.refresh_token,
        )

    def require_credentials(self) -> tuple[str, OAuthCredentials]:
        """Account id and credential set, for the adapter that connects to the venue.

        Raises:
            ValueError: The venue is not fully configured. Enabled venues are checked
                at startup, so this can only fire for a disabled one.
        """
        if (
            self.account_id is None
            or self.client_id is None
            or self.client_secret is None
            or self.access_token is None
            or self.refresh_token is None
        ):
            raise ValueError(
                "forex venue credentials were requested but the venue is not fully "
                "configured; check `enabled` before connecting"
            )
        return self.account_id, OAuthCredentials(
            client_id=self.client_id,
            client_secret=self.client_secret,
            access_token=self.access_token,
            refresh_token=self.refresh_token,
        )


@final
class CryptoVenueSettings(_Section):
    """Connection settings for one crypto exchange.

    Endpoints are configuration for the same reason the forex venue's are: mainnet and
    testnet differ only by hostname, and a constant in code makes that distinction
    invisible in review. Both are held here and neither is selected here. Which one is
    used is derived from :attr:`sandbox`, so no deployment can express the mismatch of
    testnet credentials against the mainnet endpoint.
    """

    enabled: bool
    exchange_id: NonEmptyStr
    sandbox: bool
    mainnet_rest_url: NonEmptyStr
    testnet_rest_url: NonEmptyStr
    mainnet_ws_public_url: NonEmptyStr
    testnet_ws_public_url: NonEmptyStr
    """Root of the public stream, without the product category.

    Bybit puts the category in the path, ``/v5/public/linear`` against
    ``/v5/public/spot``, and the two carry different instruments. The adapter appends
    it so that one configured root serves every category rather than needing a field
    per product.
    """
    api_key: SecretStr | None = None
    api_secret: SecretStr | None = None
    api_passphrase: SecretStr | None = None
    request_timeout_seconds: PositiveSeconds = 10.0
    max_requests_per_second: Annotated[float, Field(gt=0)] = 10.0
    """Client side cap, deliberately far below the venue's published ceiling.

    Bybit allows 600 requests per five seconds per IP, which is 120 per second, and
    answers a breach with a ten minute block rather than a retryable error. Nothing
    this system does needs that rate, so the margin is enormous on purpose: the cost of
    being slow is a few seconds on a backfill, and the cost of being blocked is ten
    minutes of no market data at all.
    """
    max_retries: Annotated[int, Field(ge=0)] = 5
    retry_backoff_seconds: PositiveSeconds = 0.5
    ws_ping_interval_seconds: PositiveSeconds = 20.0
    """Bybit documents a 20 second application level ping and cuts an idle connection
    after ten minutes. This is the venue's number, not a guess."""
    ws_receive_timeout_seconds: PositiveSeconds = 30.0
    """How long a silent socket is tolerated before it is treated as dead.

    Longer than the ping interval so that one late pong is not a disconnect, and much
    shorter than the venue's ten minute idle cut, because a stream that has stopped
    delivering is indistinguishable from a stalled one and both need a reconnect.
    """

    @model_validator(mode="after")
    def _check_endpoints(self) -> Self:
        for name, value, scheme in (
            ("mainnet_rest_url", self.mainnet_rest_url, "https://"),
            ("testnet_rest_url", self.testnet_rest_url, "https://"),
            ("mainnet_ws_public_url", self.mainnet_ws_public_url, "wss://"),
            ("testnet_ws_public_url", self.testnet_ws_public_url, "wss://"),
        ):
            if not value.startswith(scheme):
                raise ValueError(
                    f"{name} must start with {scheme}; market data crossing the network "
                    f"unencrypted can be read and altered in transit, got {value!r}"
                )
        if self.mainnet_rest_url == self.testnet_rest_url:
            raise ValueError(
                f"mainnet_rest_url and testnet_rest_url are both {self.mainnet_rest_url!r}. "
                f"The two environments hold different accounts and different balances, so "
                f"they cannot share a hostname."
            )
        if self.mainnet_ws_public_url == self.testnet_ws_public_url:
            raise ValueError(
                f"mainnet_ws_public_url and testnet_ws_public_url are both "
                f"{self.mainnet_ws_public_url!r}. Recording testnet quotes as though they "
                f"were the real market would poison the research data silently."
            )
        return self

    @property
    def rest_url(self) -> str:
        """The REST root for the configured environment.

        Derived, not configured, for the same reason as the forex venue's ``api_host``:
        there must be no field in which a testnet deployment can name the mainnet
        endpoint.
        """
        return self.testnet_rest_url if self.sandbox else self.mainnet_rest_url

    @property
    def ws_public_url(self) -> str:
        """The public stream root for the configured environment."""
        return self.testnet_ws_public_url if self.sandbox else self.mainnet_ws_public_url

    @property
    def has_credentials(self) -> bool:
        """Whether private endpoints can be reached.

        Public market data needs no credentials, so an exchange may legitimately be
        enabled without them. Execution code checks this before assuming otherwise.
        """
        return self.api_key is not None and self.api_secret is not None


@final
class VenuesSettings(_Section):
    """All configured venues, grouped by family."""

    forex: ForexVenueSettings | None = None
    crypto: dict[str, CryptoVenueSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_crypto_keys(self) -> Self:
        for name in self.crypto:
            if not name or name.strip() != name or " " in name:
                raise ValueError(
                    f"crypto venue key {name!r} must be a non-empty identifier without spaces"
                )
        return self

    def enabled_crypto(self) -> dict[str, CryptoVenueSettings]:
        return {name: venue for name, venue in self.crypto.items() if venue.enabled}

    def live_venue_names(self) -> tuple[str, ...]:
        """Names of venues configured to trade real money."""
        names: list[str] = []
        if (
            self.forex is not None
            and self.forex.enabled
            and self.forex.environment is VenueEnvironment.LIVE
        ):
            names.append("venues.forex")
        for name, venue in self.crypto.items():
            if venue.enabled and not venue.sandbox:
                names.append(f"venues.crypto.{name}")
        return tuple(names)


@dataclass(frozen=True, slots=True)
class SettingsContext:
    """Where configuration is read from, resolved before the models are built.

    Attributes:
        config_dir: Directory holding ``base.toml`` and the per-environment files.
        environment: Which environment file is layered on the base.
        secrets_dir: Directory a secret manager projects secret files into, if any.
        environ: The environment variable mapping to read. ``None`` means the live
            process environment.
    """

    config_dir: Path
    environment: Environment
    secrets_dir: Path | None
    environ: Mapping[str, str] | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> SettingsContext:
        """Resolve the context from environment variables."""
        env = os.environ if environ is None else environ
        raw_environment = env.get(f"{ENV_PREFIX}APP{ENV_NESTED_DELIMITER}ENVIRONMENT")
        environment = (
            Environment(raw_environment.strip().lower())
            if raw_environment
            else Environment.DEVELOPMENT
        )
        raw_dir = env.get(f"{ENV_PREFIX}CONFIG_DIR")
        config_dir = Path(raw_dir) if raw_dir else Path.cwd() / DEFAULT_CONFIG_DIRNAME
        raw_secrets = env.get(f"{ENV_PREFIX}SECRETS_DIR")
        return cls(
            config_dir=config_dir,
            environment=environment,
            secrets_dir=Path(raw_secrets) if raw_secrets else None,
            environ=None if environ is None else dict(environ),
        )


_active_context: ContextVar[SettingsContext | None] = ContextVar(
    "tradingsys_settings_context", default=None
)


@contextmanager
def settings_context(context: SettingsContext) -> Iterator[SettingsContext]:
    """Bind the configuration context for the duration of a block.

    Used by the loader and by tests to point settings construction at a specific
    directory and environment without mutating process state.
    """
    token = _active_context.set(context)
    try:
        yield context
    finally:
        _active_context.reset(token)


def active_context() -> SettingsContext:
    """The bound context, or one resolved from environment variables."""
    return _active_context.get() or SettingsContext.from_environ()


@final
class Settings(BaseSettings):
    """The complete, validated configuration of one process."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter=ENV_NESTED_DELIMITER,
        case_sensitive=False,
        extra="forbid",
        frozen=True,
        nested_model_default_partial_update=True,
    )

    app: AppSettings
    database: DatabaseSettings
    redis: RedisSettings
    observability: ObservabilitySettings
    venues: VenuesSettings = Field(default_factory=VenuesSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - never used, see below
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003 - replaced below
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order the layers: explicit arguments, environment, secret files, then TOML.

        ``dotenv_settings`` is dropped, permanently and in every environment. The rule
        is that this application reads secrets only from the environment or from the
        secrets directory, never from a file it parses, and that rule is worth having
        precisely because it has no exceptions. Enabling a dotenv source in development
        and test while refusing it in staging and production would make the guarantee
        conditional on the environment selector being correct, which adds a failure
        mode on the day it matters most. The cost of keeping the invariant absolute is
        one documented shell line, and ``scripts/verify.sh`` pays it for you.

        The stock secrets source is replaced because it only maps top level field
        names, which cannot express a nested secret such as ``database.password``.
        :func:`~tradingsys.config.sources.read_secrets_directory` uses the same naming
        convention as the environment variables instead.
        """
        context = active_context()
        sources: list[PydanticBaseSettingsSource] = [init_settings]
        sources.append(
            env_settings
            if context.environ is None
            else cls._mapping_source(settings_cls, context.environ)
        )
        if context.secrets_dir is not None:
            sources.append(
                cls._mapping_source(
                    settings_cls, read_secrets_directory(context.secrets_dir, ENV_PREFIX)
                )
            )
        sources.append(
            TomlLayeredSource(
                settings_cls, config_dir=context.config_dir, environment=context.environment.value
            )
        )
        return tuple(sources)

    @staticmethod
    def _mapping_source(
        settings_cls: type[BaseSettings], environ: Mapping[str, str]
    ) -> PydanticBaseSettingsSource:
        return MappingEnvSource(
            settings_cls,
            environ,
            case_sensitive=False,
            env_prefix=ENV_PREFIX,
            env_nested_delimiter=ENV_NESTED_DELIMITER,
        )

    @model_validator(mode="after")
    def _check_environment_matches_file(self) -> Self:
        expected = active_context().environment
        if self.app.environment is not expected:
            raise ValueError(
                f"app.environment is {self.app.environment.value!r} but configuration was "
                f"loaded for the {expected.value!r} environment. Set "
                f"{ENV_PREFIX}APP{ENV_NESTED_DELIMITER}ENVIRONMENT to match, or correct "
                f"app.environment in the config file."
            )
        return self

    @model_validator(mode="after")
    def _check_live_trading_is_armed(self) -> Self:
        live = self.venues.live_venue_names()
        if live and not self.app.allow_live_trading:
            raise ValueError(
                f"these venues are configured against real money: {', '.join(live)}. "
                f"Set app.allow_live_trading to true to acknowledge that deliberately."
            )
        if live and self.app.environment is not Environment.PRODUCTION:
            raise ValueError(
                f"these venues are configured against real money: {', '.join(live)}, but "
                f"app.environment is {self.app.environment.value!r}. Live venues are only "
                f"permitted in the production environment."
            )
        return self

    @model_validator(mode="after")
    def _check_production_logging(self) -> Self:
        if (
            self.app.environment is Environment.PRODUCTION
            and self.observability.log_format is not LogFormat.JSON
        ):
            raise ValueError(
                "production must log in json format; console formatting is unparseable "
                "by log ingestion and loses structured fields"
            )
        return self

    def describe(self) -> dict[str, Any]:
        """A redacted summary suitable for logging at startup.

        Secrets are represented by pydantic's ``SecretStr`` masking, so this can be
        emitted verbatim.
        """
        return {
            "app": self.app.model_dump(mode="json"),
            "database": {
                "dsn": self.database.dsn(),
                "pool": [self.database.min_pool_size, self.database.max_pool_size],
            },
            "redis": {"url": self.redis.url()},
            "observability": self.observability.model_dump(mode="json"),
            "venues": {
                "forex": (
                    None
                    if self.venues.forex is None
                    else {
                        "enabled": self.venues.forex.enabled,
                        "environment": self.venues.forex.environment.value,
                        "endpoint": (f"{self.venues.forex.api_host}:{self.venues.forex.api_port}"),
                        "token_url": self.venues.forex.token_url,
                        "account_id": self.venues.forex.account_id,
                        "has_credentials": self.venues.forex.has_credentials,
                    }
                ),
                "crypto": {
                    name: {
                        "enabled": venue.enabled,
                        "exchange_id": venue.exchange_id,
                        "sandbox": venue.sandbox,
                        "has_credentials": venue.has_credentials,
                    }
                    for name, venue in self.venues.crypto.items()
                },
            },
        }
