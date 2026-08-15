"""Tests for configuration sources: TOML layering and the secrets-in-files guard."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, SecretStr

from tradingsys.config.settings import Settings
from tradingsys.config.sources import (
    TomlLayeredSource,
    deep_merge,
    read_secrets_directory,
    reject_secrets_from_files,
    resolve_config_dir,
    secret_field_paths,
)
from tradingsys.core.errors import ConfigurationError, SecretInConfigFileError

if TYPE_CHECKING:
    from pathlib import Path


class TestDeepMerge:
    def test_overlay_wins_for_scalars(self) -> None:
        assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_mappings_combine(self) -> None:
        base = {"db": {"host": "localhost", "port": 5432}}
        overlay = {"db": {"host": "prod"}}
        assert deep_merge(base, overlay) == {"db": {"host": "prod", "port": 5432}}

    def test_deeply_nested_mappings_combine(self) -> None:
        base = {"venues": {"crypto": {"binance": {"enabled": False, "sandbox": True}}}}
        overlay = {"venues": {"crypto": {"binance": {"enabled": True}}}}
        assert deep_merge(base, overlay) == {
            "venues": {"crypto": {"binance": {"enabled": True, "sandbox": True}}}
        }

    def test_lists_are_replaced_not_appended(self) -> None:
        # An environment must be able to shorten an inherited list.
        assert deep_merge({"x": [1, 2, 3]}, {"x": [9]}) == {"x": [9]}

    def test_new_keys_are_added(self) -> None:
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_inputs_are_not_mutated(self) -> None:
        base = {"db": {"host": "localhost"}}
        deep_merge(base, {"db": {"host": "prod"}})
        assert base == {"db": {"host": "localhost"}}


class TestSecretFieldDiscovery:
    def test_finds_nested_secret_fields_of_the_real_settings_model(self) -> None:
        paths = secret_field_paths(Settings)
        assert ("database", "password") in paths
        assert ("venues", "forex", "client_secret") in paths
        assert ("venues", "forex", "access_token") in paths
        assert ("venues", "forex", "refresh_token") in paths
        assert ("venues", "crypto", "api_key") in paths
        assert ("venues", "crypto", "api_secret") in paths

    def test_finds_optional_secrets(self) -> None:
        assert ("redis", "password") in secret_field_paths(Settings)

    def test_ignores_non_secret_fields(self) -> None:
        paths = secret_field_paths(Settings)
        assert ("database", "host") not in paths
        assert ("venues", "forex", "account_id") not in paths

    def test_handles_a_model_without_secrets(self) -> None:
        class Plain(BaseModel):
            value: int

        assert secret_field_paths(Plain) == frozenset()

    def test_handles_a_non_model(self) -> None:
        assert secret_field_paths(int) == frozenset()


class TestSecretRejection:
    def test_plain_data_is_accepted(self) -> None:
        # A file full of legitimate non-secret settings, including account_id, which
        # sits right beside four secrets in the same section, passes untouched. The
        # differential matters more than the acceptance: the same payload with one
        # secret added is refused, so what is being detected is the secret and not
        # merely the shape of the data.
        clean = {
            "database": {"host": "db", "port": 5432, "user": "tradingsys"},
            "venues": {"forex": {"enabled": True, "account_id": "5325402"}},
        }
        reject_secrets_from_files(clean, Settings, "base.toml")

        contaminated = {
            "database": {"host": "db", "port": 5432, "user": "tradingsys"},
            "venues": {"forex": {"enabled": True, "account_id": "5325402", "client_secret": "x"}},
        }
        with pytest.raises(SecretInConfigFileError, match=r"venues\.forex\.client_secret"):
            reject_secrets_from_files(contaminated, Settings, "base.toml")

    def test_a_top_level_secret_is_rejected(self) -> None:
        with pytest.raises(SecretInConfigFileError, match=r"database\.password"):
            reject_secrets_from_files({"database": {"password": "hunter2"}}, Settings, "base.toml")

    def test_a_nested_venue_secret_is_rejected(self) -> None:
        with pytest.raises(SecretInConfigFileError, match=r"venues\.forex\.client_secret"):
            reject_secrets_from_files(
                {"venues": {"forex": {"client_secret": "abc"}}}, Settings, "base.toml"
            )

    def test_a_secret_under_an_arbitrarily_keyed_map_is_rejected(self) -> None:
        with pytest.raises(SecretInConfigFileError, match=r"venues\.crypto\.api_key"):
            reject_secrets_from_files(
                {"venues": {"crypto": {"bybit": {"api_key": "abc"}}}}, Settings, "base.toml"
            )

    def test_every_offender_is_listed(self) -> None:
        with pytest.raises(SecretInConfigFileError) as caught:
            reject_secrets_from_files(
                {
                    "database": {"password": "a"},
                    "redis": {"password": "b"},
                    "venues": {"forex": {"client_secret": "c"}},
                },
                Settings,
                "base.toml",
            )
        message = str(caught.value)
        assert "database.password" in message
        assert "redis.password" in message
        assert "venues.forex.client_secret" in message

    def test_the_error_names_the_file_and_suggests_an_env_var(self) -> None:
        with pytest.raises(SecretInConfigFileError) as caught:
            reject_secrets_from_files(
                {"database": {"password": "a"}}, Settings, "/etc/tradingsys/base.toml"
            )
        message = str(caught.value)
        assert "/etc/tradingsys/base.toml" in message
        assert "TRADINGSYS_DATABASE__PASSWORD" in message

    def test_an_empty_secret_value_is_still_rejected(self) -> None:
        # Presence is what matters: an empty password in a file is still a credential
        # in a file, and the next commit may fill it in.
        with pytest.raises(SecretInConfigFileError):
            reject_secrets_from_files({"database": {"password": ""}}, Settings, "base.toml")


class TestTomlLayeredSource:
    def test_reads_base_only(self, tmp_path: Path) -> None:
        (tmp_path / "base.toml").write_text('[app]\nname = "base"\n')
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="development")
        assert source() == {"app": {"name": "base"}}

    def test_environment_file_is_layered_on_top(self, tmp_path: Path) -> None:
        (tmp_path / "base.toml").write_text('[app]\nname = "base"\nshutdown_grace_seconds = 5.0\n')
        (tmp_path / "production.toml").write_text('[app]\nname = "prod"\n')
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="production")
        assert source() == {"app": {"name": "prod", "shutdown_grace_seconds": 5.0}}

    def test_a_missing_environment_file_is_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "base.toml").write_text('[app]\nname = "base"\n')
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="staging")
        assert source() == {"app": {"name": "base"}}

    def test_a_missing_base_file_is_fatal(self, tmp_path: Path) -> None:
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="development")
        with pytest.raises(ConfigurationError, match="required configuration file"):
            source()

    def test_a_missing_directory_is_fatal(self, tmp_path: Path) -> None:
        source = TomlLayeredSource(
            Settings, config_dir=tmp_path / "nope", environment="development"
        )
        with pytest.raises(ConfigurationError, match="does not exist"):
            source()

    def test_malformed_toml_is_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "base.toml").write_text("[app\nname = ")
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="development")
        with pytest.raises(ConfigurationError, match="not valid TOML"):
            source()

    def test_secrets_in_the_base_file_are_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "base.toml").write_text('[database]\npassword = "hunter2"\n')
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="development")
        with pytest.raises(SecretInConfigFileError, match=r"database\.password"):
            source()

    def test_secrets_in_the_environment_file_are_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "base.toml").write_text('[app]\nname = "base"\n')
        (tmp_path / "production.toml").write_text('[venues.forex]\nclient_secret = "abc"\n')
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="production")
        with pytest.raises(SecretInConfigFileError, match=r"venues\.forex\.client_secret"):
            source()

    def test_the_file_is_read_once(self, tmp_path: Path) -> None:
        path = tmp_path / "base.toml"
        path.write_text('[app]\nname = "first"\n')
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="development")
        first = source()
        path.write_text('[app]\nname = "second"\n')
        assert source() == first

    def test_repr_names_the_directory_and_environment(self, tmp_path: Path) -> None:
        source = TomlLayeredSource(Settings, config_dir=tmp_path, environment="staging")
        assert "staging" in repr(source)
        assert str(tmp_path) in repr(source)


class TestReadSecretsDirectory:
    def test_a_prefixed_file_name_is_used_as_is(self, tmp_path: Path) -> None:
        (tmp_path / "TRADINGSYS_DATABASE__PASSWORD").write_text("hunter2")
        assert read_secrets_directory(tmp_path, "TRADINGSYS_") == {
            "TRADINGSYS_DATABASE__PASSWORD": "hunter2"
        }

    def test_an_unprefixed_file_name_gains_the_prefix(self, tmp_path: Path) -> None:
        (tmp_path / "database__password").write_text("hunter2")
        assert read_secrets_directory(tmp_path, "TRADINGSYS_") == {
            "TRADINGSYS_DATABASE__PASSWORD": "hunter2"
        }

    def test_one_trailing_newline_is_stripped(self, tmp_path: Path) -> None:
        (tmp_path / "database__password").write_text("hunter2\n")
        assert read_secrets_directory(tmp_path, "TRADINGSYS_") == {
            "TRADINGSYS_DATABASE__PASSWORD": "hunter2"
        }

    def test_interior_whitespace_is_preserved(self, tmp_path: Path) -> None:
        (tmp_path / "database__password").write_text("  spaced secret  \n")
        secrets = read_secrets_directory(tmp_path, "TRADINGSYS_")
        assert secrets["TRADINGSYS_DATABASE__PASSWORD"] == "  spaced secret  "

    def test_kubernetes_projection_artefacts_are_skipped(self, tmp_path: Path) -> None:
        # A projected secret volume contains ..data -> ..2024_01_01/ and dotted dirs.
        (tmp_path / "..2024_01_01").mkdir()
        (tmp_path / "..2024_01_01" / "database__password").write_text("hunter2")
        (tmp_path / "database__password").write_text("hunter2")
        assert read_secrets_directory(tmp_path, "TRADINGSYS_") == {
            "TRADINGSYS_DATABASE__PASSWORD": "hunter2"
        }

    def test_subdirectories_are_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "nested").mkdir()
        assert read_secrets_directory(tmp_path, "TRADINGSYS_") == {}

    def test_an_empty_directory_yields_nothing(self, tmp_path: Path) -> None:
        assert read_secrets_directory(tmp_path, "TRADINGSYS_") == {}

    def test_a_missing_directory_is_fatal(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="is not a directory"):
            read_secrets_directory(tmp_path / "absent", "TRADINGSYS_")


class TestResolveConfigDir:
    def test_returns_the_first_existing_directory(self, tmp_path: Path) -> None:
        second = tmp_path / "second"
        second.mkdir()
        assert resolve_config_dir([tmp_path / "first", second]) == second

    def test_raises_when_nothing_exists(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="no configuration directory found"):
            resolve_config_dir([tmp_path / "a", tmp_path / "b"])


class TestSecretStrIsActuallyRedacted:
    def test_pydantic_masks_secrets_in_repr(self) -> None:
        secret = SecretStr("hunter2")
        assert "hunter2" not in repr(secret)
        assert "hunter2" not in str(secret)
        assert secret.get_secret_value() == "hunter2"
