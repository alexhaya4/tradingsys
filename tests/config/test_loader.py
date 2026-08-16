"""Tests for startup configuration loading.

These exercise the real settings model against real files on disk. The layering,
the precedence order, and the failure messages are all things an operator depends on
at three in the morning, so they are asserted rather than assumed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tradingsys.config.loader import env_var_for, load_settings
from tradingsys.config.settings import (
    Environment,
    LogFormat,
    LogLevel,
    Settings,
    VenueEnvironment,
)
from tradingsys.core.errors import ConfigurationError, SecretInConfigFileError

if TYPE_CHECKING:
    from collections.abc import Mapping

REPO_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

MINIMAL_BASE = """
[app]
name = "tradingsys"
environment = "development"
shutdown_grace_seconds = 20.0

[database]
host = "localhost"
port = 5432
database = "tradingsys"
user = "tradingsys"
min_pool_size = 2
max_pool_size = 10
connect_timeout_seconds = 10.0
command_timeout_seconds = 30.0
statement_cache_size = 0
ssl_mode = "prefer"

[redis]
host = "localhost"
port = 6379
db = 0

[observability]
service_name = "tradingsys"
log_level = "info"
log_format = "json"
http_host = "0.0.0.0"
http_port = 8000
health_path = "/health"
ready_path = "/ready"
metrics_path = "/metrics"
readiness_timeout_seconds = 3.0
"""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    (tmp_path / "base.toml").write_text(MINIMAL_BASE)
    return tmp_path


def env(**overrides: str) -> dict[str, str]:
    """An environment mapping with the database password always supplied."""
    return {"TRADINGSYS_DATABASE__PASSWORD": "s3cret", **overrides}


def load(config_dir: Path, environ: Mapping[str, str] | None = None, **kwargs: object) -> Settings:
    return load_settings(
        config_dir=config_dir,
        environ=dict(environ) if environ is not None else env(),
        **kwargs,  # type: ignore[arg-type]
    )


class TestLayering:
    def test_defaults_come_from_the_model(self, config_dir: Path) -> None:
        settings = load(config_dir)
        # allow_live_trading is declared only on the model, not in the file.
        assert settings.app.allow_live_trading is False

    def test_values_come_from_the_base_file(self, config_dir: Path) -> None:
        assert load(config_dir).database.port == 5432

    def test_the_environment_file_overrides_the_base_file(self, config_dir: Path) -> None:
        (config_dir / "staging.toml").write_text(
            '[app]\nenvironment = "staging"\n\n[database]\nport = 6543\n'
        )
        settings = load(config_dir, environment=Environment.STAGING)
        assert settings.database.port == 6543
        assert settings.database.host == "localhost"  # inherited from base

    def test_environment_variables_override_files(self, config_dir: Path) -> None:
        settings = load(config_dir, env(TRADINGSYS_DATABASE__PORT="7000"))
        assert settings.database.port == 7000

    def test_environment_variables_reach_deeply_nested_fields(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE
            + """
[venues.crypto.bybit]
enabled = false
exchange_id = "bybit"
sandbox = true
mainnet_rest_url = "https://api.bybit.com"
testnet_rest_url = "https://api-testnet.bybit.com"
mainnet_ws_public_url = "wss://stream.bybit.com/v5/public"
testnet_ws_public_url = "wss://stream-testnet.bybit.com/v5/public"
"""
        )
        settings = load(config_dir, env(TRADINGSYS_VENUES__CRYPTO__BYBIT__API_KEY="key-from-env"))
        bybit = settings.venues.crypto["bybit"]
        assert bybit.api_key is not None
        assert bybit.api_key.get_secret_value() == "key-from-env"
        assert bybit.exchange_id == "bybit"  # still from the file

    def test_a_secret_from_the_secrets_directory_is_used(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "database__password").write_text("from-secret-manager")
        settings = load_settings(config_dir=config_dir, secrets_dir=secrets, environ={})
        assert settings.database.password.get_secret_value() == "from-secret-manager"

    def test_environment_variables_beat_the_secrets_directory(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        secrets = tmp_path / "secrets"
        secrets.mkdir()
        (secrets / "database__password").write_text("from-file")
        settings = load_settings(
            config_dir=config_dir,
            secrets_dir=secrets,
            environ=env(TRADINGSYS_DATABASE__PASSWORD="from-env"),
        )
        assert settings.database.password.get_secret_value() == "from-env"

    def test_a_missing_secrets_directory_is_fatal(self, config_dir: Path, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="secrets directory"):
            load_settings(config_dir=config_dir, secrets_dir=tmp_path / "absent", environ=env())


class TestEnvironmentSelection:
    def test_environment_defaults_to_development(self, config_dir: Path) -> None:
        assert load(config_dir).app.environment is Environment.DEVELOPMENT

    def test_environment_comes_from_the_environment_variable(self, config_dir: Path) -> None:
        (config_dir / "test.toml").write_text('[app]\nenvironment = "test"\n')
        settings = load(config_dir, env(TRADINGSYS_APP__ENVIRONMENT="test"))
        assert settings.app.environment is Environment.TEST

    def test_an_explicit_argument_wins(self, config_dir: Path) -> None:
        (config_dir / "test.toml").write_text('[app]\nenvironment = "test"\n')
        settings = load_settings(config_dir=config_dir, environment=Environment.TEST, environ=env())
        assert settings.app.environment is Environment.TEST

    def test_a_file_declaring_a_different_environment_is_rejected(self, config_dir: Path) -> None:
        # base.toml says development; loading it as staging is a config mistake.
        with pytest.raises(ConfigurationError, match="but configuration was loaded for"):
            load(config_dir, environment=Environment.STAGING)

    def test_an_unknown_environment_name_is_rejected(self, config_dir: Path) -> None:
        with pytest.raises(ValueError, match="not a valid Environment"):
            load(config_dir, env(TRADINGSYS_APP__ENVIRONMENT="qa"))


class TestFailureReporting:
    def test_a_missing_required_value_is_reported(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError) as caught:
            load(config_dir, environ={})
        message = str(caught.value)
        assert "database.password" in message
        assert "TRADINGSYS_DATABASE__PASSWORD" in message

    def test_every_invalid_field_is_reported_at_once(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError) as caught:
            load(
                config_dir,
                env(TRADINGSYS_DATABASE__PORT="99999", TRADINGSYS_OBSERVABILITY__LOG_LEVEL="loud"),
            )
        message = str(caught.value)
        assert "database.port" in message
        assert "observability.log_level" in message

    def test_the_report_names_the_environment_and_directory(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError) as caught:
            load(config_dir, environ={})
        message = str(caught.value)
        assert "development" in message
        assert str(config_dir) in message

    def test_an_unknown_key_is_rejected(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE.replace('user = "tradingsys"', 'user = "tradingsys"\ntypoed_key = 1')
        )
        with pytest.raises(ConfigurationError, match="typoed_key"):
            load(config_dir)

    def test_an_unknown_top_level_section_is_rejected(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(MINIMAL_BASE + "\n[nonsense]\nvalue = 1\n")
        with pytest.raises(ConfigurationError, match="nonsense"):
            load(config_dir)

    def test_a_malformed_value_is_rejected(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match=r"database\.port"):
            load(config_dir, env(TRADINGSYS_DATABASE__PORT="not-a-number"))

    def test_env_var_naming(self) -> None:
        assert env_var_for("database.password") == "TRADINGSYS_DATABASE__PASSWORD"
        assert (
            env_var_for("venues.crypto.bybit.api_key")
            == "TRADINGSYS_VENUES__CRYPTO__BYBIT__API_KEY"
        )


class TestSecretsNeverComeFromFiles:
    def test_a_password_in_the_base_file_aborts_startup(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE.replace('user = "tradingsys"', 'user = "tradingsys"\npassword = "oops"')
        )
        with pytest.raises(SecretInConfigFileError, match=r"database\.password"):
            load(config_dir)

    def test_a_venue_token_in_an_environment_file_aborts_startup(self, config_dir: Path) -> None:
        (config_dir / "staging.toml").write_text(
            '[app]\nenvironment = "staging"\n\n[venues.forex]\nclient_secret = "oops"\n'
        )
        with pytest.raises(SecretInConfigFileError, match=r"venues\.forex\.client_secret"):
            load(config_dir, environment=Environment.STAGING)


class TestValidationRules:
    def test_pool_bounds(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="exceeds max_pool_size"):
            load(config_dir, env(TRADINGSYS_DATABASE__MIN_POOL_SIZE="50"))

    def test_operational_paths_must_be_absolute(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="must start with"):
            load(config_dir, env(TRADINGSYS_OBSERVABILITY__HEALTH_PATH="health"))

    def test_operational_paths_must_differ(self, config_dir: Path) -> None:
        with pytest.raises(ConfigurationError, match="must differ"):
            load(config_dir, env(TRADINGSYS_OBSERVABILITY__READY_PATH="/health"))

    def test_production_must_log_json(self, config_dir: Path) -> None:
        (config_dir / "production.toml").write_text(
            '[app]\nenvironment = "production"\n\n[observability]\nlog_format = "console"\n'
        )
        with pytest.raises(ConfigurationError, match="production must log in json"):
            load(config_dir, environment=Environment.PRODUCTION)

    def test_the_token_url_must_be_https(self, config_dir: Path) -> None:
        # A client secret is posted to this endpoint, so plaintext http would put it on
        # the wire in the clear.
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE
            + _forex_section(enabled=False).replace(
                "https://openapi.example.com/apps/token", "http://openapi.example.com/apps/token"
            )
        )
        with pytest.raises(ConfigurationError, match="must be an https URL"):
            load(config_dir)

    def test_enabling_a_venue_without_credentials_is_rejected(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(MINIMAL_BASE + _forex_section(enabled=True))
        with pytest.raises(ConfigurationError) as caught:
            load(config_dir)
        message = str(caught.value)
        # Every absent field is named, not just the first: supplying five variables one
        # error at a time is a miserable way to configure a venue.
        for field in ("account_id", "client_id", "client_secret", "access_token", "refresh_token"):
            assert field in message

    def test_a_partially_configured_venue_names_only_what_is_absent(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(MINIMAL_BASE + _forex_section(enabled=True))
        supplied = dict(FOREX_CREDENTIALS)
        del supplied["TRADINGSYS_VENUES__FOREX__REFRESH_TOKEN"]
        with pytest.raises(ConfigurationError) as caught:
            load(config_dir, env(**supplied))
        message = str(caught.value)
        assert "refresh_token" in message
        assert "client_secret" not in message
        assert "TRADINGSYS_VENUES__FOREX__REFRESH_TOKEN" in message

    def test_enabling_a_venue_with_credentials_from_the_environment_works(
        self, config_dir: Path
    ) -> None:
        (config_dir / "base.toml").write_text(MINIMAL_BASE + _forex_section(enabled=True))
        settings = load(config_dir, env(**FOREX_CREDENTIALS))
        assert settings.venues.forex is not None
        assert settings.venues.forex.has_credentials
        account_id, credentials = settings.venues.forex.require_credentials()
        assert account_id == "9999999"
        assert credentials.client_id.get_secret_value() == "client-id"
        assert credentials.client_secret.get_secret_value() == "client-secret"
        assert credentials.access_token.get_secret_value() == "access-token"
        assert credentials.refresh_token.get_secret_value() == "refresh-token"

    def test_a_disabled_venue_refuses_to_hand_out_credentials(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(MINIMAL_BASE + _forex_section(enabled=False))
        settings = load(config_dir)
        assert settings.venues.forex is not None
        assert not settings.venues.forex.has_credentials
        with pytest.raises(ValueError, match="not fully configured"):
            settings.venues.forex.require_credentials()


class TestTheEndpointCannotBeMismatched:
    """The demo and live endpoints are fully separated at the venue.

    A live endpoint cannot serve a demo account or the reverse, so the configuration
    must not be able to express the combination at all.
    """

    def test_a_practice_environment_selects_the_demo_host(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE + _forex_section(enabled=False, venue_environment="practice")
        )
        forex = load(config_dir).venues.forex
        assert forex is not None
        assert forex.api_host == "demo.example.com"

    def test_a_live_environment_selects_the_live_host(self, config_dir: Path) -> None:
        (config_dir / "production.toml").write_text(
            '[app]\nenvironment = "production"\nallow_live_trading = true\n'
            + _forex_section(enabled=False, venue_environment="live")
        )
        forex = load(config_dir, environment=Environment.PRODUCTION).venues.forex
        assert forex is not None
        assert forex.api_host == "live.example.com"

    def test_there_is_no_field_in_which_to_express_a_mismatch(self, config_dir: Path) -> None:
        # The host is derived from the environment, so a config that tries to name one
        # directly is rejected as an unknown key rather than silently honoured.
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE + _forex_section(enabled=False) + '\napi_host = "live.example.com"\n'
        )
        with pytest.raises(ConfigurationError, match="api_host"):
            load(config_dir)

    def test_the_two_hosts_must_differ(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE
            + _forex_section(enabled=False).replace(
                'live_api_host = "live.example.com"', 'live_api_host = "demo.example.com"'
            )
        )
        with pytest.raises(ConfigurationError, match="cannot share a hostname"):
            load(config_dir)

    def test_the_shipped_configuration_separates_them(self) -> None:
        practice = load_settings(
            config_dir=REPO_CONFIG_DIR,
            environment=Environment.STAGING,
            environ=env(**FOREX_CREDENTIALS),
        ).venues.forex
        live = load_settings(
            config_dir=REPO_CONFIG_DIR, environment=Environment.PRODUCTION, environ=env()
        ).venues.forex
        assert practice is not None
        assert live is not None
        assert practice.api_host == "demo.ctraderapi.com"
        assert live.api_host == "live.ctraderapi.com"

    def test_the_protobuf_port_is_configured(self) -> None:
        # 5035 is protobuf only; 5036 is the JSON port, which this client does not speak.
        forex = load_settings(
            config_dir=REPO_CONFIG_DIR,
            environment=Environment.STAGING,
            environ=env(**FOREX_CREDENTIALS),
        ).venues.forex
        assert forex is not None
        assert forex.api_port == 5035


class TestLiveTradingSafety:
    def _live_config(self, config_dir: Path, *, allow: bool, environment: str) -> None:
        (config_dir / f"{environment}.toml").write_text(
            f'[app]\nenvironment = "{environment}"\nallow_live_trading = {str(allow).lower()}\n'
            + _forex_section(enabled=True, venue_environment="live")
        )

    def test_a_live_venue_requires_the_arming_flag(self, config_dir: Path) -> None:
        self._live_config(config_dir, allow=False, environment="production")
        with pytest.raises(ConfigurationError, match="allow_live_trading"):
            load(
                config_dir,
                env(**FOREX_CREDENTIALS),
                environment=Environment.PRODUCTION,
            )

    def test_a_live_venue_is_refused_outside_production(self, config_dir: Path) -> None:
        self._live_config(config_dir, allow=True, environment="development")
        with pytest.raises(ConfigurationError, match="only permitted in the production"):
            load(
                config_dir,
                env(**FOREX_CREDENTIALS),
            )

    def test_an_armed_live_venue_in_production_is_accepted(self, config_dir: Path) -> None:
        self._live_config(config_dir, allow=True, environment="production")
        settings = load(
            config_dir,
            env(**FOREX_CREDENTIALS),
            environment=Environment.PRODUCTION,
        )
        assert settings.venues.live_venue_names() == ("venues.forex",)

    def test_a_non_sandbox_crypto_venue_counts_as_live(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE
            + """
[venues.crypto.bybit]
enabled = true
exchange_id = "bybit"
sandbox = false
mainnet_rest_url = "https://api.bybit.com"
testnet_rest_url = "https://api-testnet.bybit.com"
mainnet_ws_public_url = "wss://stream.bybit.com/v5/public"
testnet_ws_public_url = "wss://stream-testnet.bybit.com/v5/public"
"""
        )
        with pytest.raises(ConfigurationError, match=r"venues\.crypto\.bybit"):
            load(config_dir)

    def test_a_practice_venue_needs_no_arming(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(
            MINIMAL_BASE + _forex_section(enabled=True, venue_environment="practice")
        )
        settings = load(
            config_dir,
            env(**FOREX_CREDENTIALS),
        )
        assert settings.venues.live_venue_names() == ()


class TestRedaction:
    def test_the_database_dsn_hides_the_password(self, config_dir: Path) -> None:
        settings = load(config_dir, env(TRADINGSYS_DATABASE__PASSWORD="hunter2"))
        assert "hunter2" not in settings.database.dsn()
        assert "hunter2" in settings.database.dsn(reveal_password=True)

    def test_special_characters_in_the_password_are_url_encoded(self, config_dir: Path) -> None:
        settings = load(config_dir, env(TRADINGSYS_DATABASE__PASSWORD="p@ss:w/rd"))
        assert "p%40ss%3Aw%2Frd" in settings.database.dsn(reveal_password=True)

    def test_the_redis_url_hides_the_password(self, config_dir: Path) -> None:
        settings = load(config_dir, env(TRADINGSYS_REDIS__PASSWORD="hunter2"))
        assert "hunter2" not in settings.redis.url()
        assert "hunter2" in settings.redis.url(reveal_password=True)

    def test_describe_never_leaks_a_secret(self, config_dir: Path) -> None:
        (config_dir / "base.toml").write_text(MINIMAL_BASE + _forex_section(enabled=True))
        settings = load(
            config_dir,
            env(
                TRADINGSYS_DATABASE__PASSWORD="db-secret",
                TRADINGSYS_REDIS__PASSWORD="redis-secret",
                TRADINGSYS_VENUES__FOREX__ACCOUNT_ID="acct",
                TRADINGSYS_VENUES__FOREX__CLIENT_ID="client-secret-value",
                TRADINGSYS_VENUES__FOREX__CLIENT_SECRET="venue-secret",
                TRADINGSYS_VENUES__FOREX__ACCESS_TOKEN="access-secret",
                TRADINGSYS_VENUES__FOREX__REFRESH_TOKEN="refresh-secret",
            ),
        )
        rendered = repr(settings.describe())
        for secret in (
            "db-secret",
            "redis-secret",
            "venue-secret",
            "access-secret",
            "refresh-secret",
        ):
            assert secret not in rendered

    def test_the_settings_repr_never_leaks_a_secret(self, config_dir: Path) -> None:
        settings = load(config_dir, env(TRADINGSYS_DATABASE__PASSWORD="db-secret"))
        assert "db-secret" not in repr(settings)


class TestImmutability:
    def test_settings_are_frozen(self, config_dir: Path) -> None:
        settings = load(config_dir)
        with pytest.raises(ValueError, match="frozen"):
            settings.app = settings.app  # type: ignore[misc]


class TestShippedConfiguration:
    """The config directory in this repository must actually load."""

    @pytest.mark.parametrize(
        "environment",
        [Environment.DEVELOPMENT, Environment.TEST, Environment.STAGING, Environment.PRODUCTION],
    )
    def test_each_shipped_environment_loads(self, environment: Environment) -> None:
        settings = load_settings(
            config_dir=REPO_CONFIG_DIR,
            environment=environment,
            environ=env(**FOREX_CREDENTIALS),
        )
        assert settings.app.environment is environment

    def test_no_shipped_file_contains_a_secret(self) -> None:
        for path in sorted(REPO_CONFIG_DIR.glob("*.toml")):
            contents = path.read_text()
            for forbidden in (
                "password",
                "client_secret",
                "access_token",
                "refresh_token",
                "api_secret",
                "api_key",
            ):
                assert f"{forbidden} =" not in contents, f"{path} sets {forbidden}"

    def test_shipped_production_does_not_arm_live_trading(self) -> None:
        settings = load_settings(
            config_dir=REPO_CONFIG_DIR, environment=Environment.PRODUCTION, environ=env()
        )
        assert settings.app.allow_live_trading is False
        assert settings.venues.live_venue_names() == ()

    def test_shipped_development_is_readable_by_a_human(self) -> None:
        settings = load_settings(
            config_dir=REPO_CONFIG_DIR, environment=Environment.DEVELOPMENT, environ=env()
        )
        assert settings.observability.log_format is LogFormat.CONSOLE
        assert settings.observability.log_level is LogLevel.DEBUG

    def test_shipped_staging_points_at_practice_venues(self) -> None:
        settings = load_settings(
            config_dir=REPO_CONFIG_DIR,
            environment=Environment.STAGING,
            environ=env(**FOREX_CREDENTIALS),
        )
        assert settings.venues.forex is not None
        assert settings.venues.forex.environment is VenueEnvironment.PRACTICE
        assert settings.venues.crypto["bybit"].sandbox is True


def _forex_section(*, enabled: bool, venue_environment: str = "practice") -> str:
    return f"""
[venues.forex]
enabled = {str(enabled).lower()}
environment = "{venue_environment}"
demo_api_host = "demo.example.com"
live_api_host = "live.example.com"
api_port = 5035
token_url = "https://openapi.example.com/apps/token"
request_timeout_seconds = 10.0
stream_read_timeout_seconds = 20.0
max_retries = 3
retry_backoff_seconds = 0.5
max_requests_per_second = 30.0
heartbeat_interval_seconds = 10.0
token_refresh_margin_seconds = 259200.0
"""


# The five variables an enabled forex venue needs, as the environment supplies them.
FOREX_CREDENTIALS = {
    "TRADINGSYS_VENUES__FOREX__ACCOUNT_ID": "9999999",
    "TRADINGSYS_VENUES__FOREX__CLIENT_ID": "client-id",
    "TRADINGSYS_VENUES__FOREX__CLIENT_SECRET": "client-secret",
    "TRADINGSYS_VENUES__FOREX__ACCESS_TOKEN": "access-token",
    "TRADINGSYS_VENUES__FOREX__REFRESH_TOKEN": "refresh-token",
}
