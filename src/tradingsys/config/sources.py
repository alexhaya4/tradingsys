"""Configuration sources and the rules that govern them.

Two things live here that pydantic-settings does not give us directly:

* A TOML source that layers a shared ``base.toml`` under a per-environment file, so
  that an environment override only has to state what differs.
* A guard that refuses to load a secret from a file. Every field typed ``SecretStr``
  is discovered by walking the settings model, and if a config file supplies one the
  process aborts. Secrets come from the environment or from a secrets directory that
  a secret manager writes into, and nowhere else.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, final

from pydantic import SecretBytes, SecretStr
from pydantic_settings import BaseSettings, EnvSettingsSource, PydanticBaseSettingsSource
from pydantic_settings.sources.utils import parse_env_vars

from tradingsys.core.errors import ConfigurationError, SecretInConfigFileError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pydantic.fields import FieldInfo

__all__ = [
    "MappingEnvSource",
    "TomlLayeredSource",
    "deep_merge",
    "read_secrets_directory",
    "reject_secrets_from_files",
    "secret_field_paths",
]

_SECRET_TYPES: tuple[type, ...] = (SecretStr, SecretBytes)


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` onto ``base``, recursing into nested mappings.

    Values other than mappings replace rather than combine. In particular a list in an
    overlay replaces the list beneath it: appending would make it impossible for an
    environment to shorten a list it inherited.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def secret_field_paths(model: type[BaseSettings] | Any) -> frozenset[tuple[str, ...]]:
    """Every dotted path in ``model`` whose field holds a secret.

    Walks nested models so that ``venues.forex.api_token`` is found, not only top level
    fields. Paths are returned as tuples of field names.
    """
    return frozenset(_walk_secret_fields(model, prefix=()))


def _walk_secret_fields(model: Any, prefix: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    fields: Mapping[str, FieldInfo] | None = getattr(model, "model_fields", None)
    if fields is None:
        return
    for name, field in fields.items():
        path = (*prefix, name)
        for annotation in _unwrap_annotation(field.annotation):
            if isinstance(annotation, type) and issubclass(annotation, _SECRET_TYPES):
                yield path
            elif hasattr(annotation, "model_fields"):
                yield from _walk_secret_fields(annotation, path)


def _unwrap_annotation(annotation: Any) -> Iterator[Any]:
    """Yield the concrete types inside an annotation, seeing through unions and generics.

    ``SecretStr | None`` and ``dict[str, VenueSettings]`` both have to be seen through,
    otherwise an optional secret or a secret inside a keyed collection would be missed.
    """
    if annotation is None:
        return
    args = getattr(annotation, "__args__", None)
    if args:
        for arg in args:
            yield from _unwrap_annotation(arg)
        return
    yield annotation


def reject_secrets_from_files(
    data: Mapping[str, Any], model: type[BaseSettings], origin: str
) -> None:
    """Abort if ``data`` supplies any field that the model types as a secret.

    Args:
        data: The nested mapping loaded from configuration files.
        model: The settings model whose secret fields define what is forbidden.
        origin: Human readable description of where the data came from, used in the
            error message.

    Raises:
        SecretInConfigFileError: One or more secret fields were present.
    """
    offenders = sorted(
        ".".join(path) for path in secret_field_paths(model) if _contains_path(data, path)
    )
    if not offenders:
        return
    listed = ", ".join(offenders)
    raise SecretInConfigFileError(
        f"{origin} sets secret values that must never live in a configuration file: "
        f"{listed}. Provide them through environment variables (for example "
        f"{_env_hint(offenders[0])}) or through the secrets directory."
    )


def _contains_path(data: Mapping[str, Any], path: tuple[str, ...]) -> bool:
    """Whether a nested mapping has a value at ``path``.

    Dictionaries keyed by arbitrary names, such as the per-exchange crypto venue map,
    are searched one level deeper so that ``venues.crypto.binance.api_key`` is caught
    by the ``venues.crypto.api_key`` pattern.
    """
    head, *rest = path
    if head not in data:
        # The path may sit under an arbitrarily keyed sub-mapping.
        return any(
            _contains_path(value, path) for value in data.values() if isinstance(value, dict)
        )
    value = data[head]
    if not rest:
        return True
    if isinstance(value, dict):
        return _contains_path(value, tuple(rest))
    return False


def _env_hint(dotted_path: str) -> str:
    """The environment variable that would supply a given dotted field path."""
    return "TRADINGSYS_" + dotted_path.upper().replace(".", "__")


@final
class MappingEnvSource(EnvSettingsSource):
    """An environment variable source reading from an explicit mapping.

    The default source reads :data:`os.environ` at construction time, which makes the
    resolved configuration depend on ambient process state. Taking the mapping as an
    argument lets the loader be called with an exact environment, which is what tests
    and one-shot administrative commands need, without any patching of globals.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        environ: Mapping[str, str],
        *,
        case_sensitive: bool | None = None,
        env_prefix: str | None = None,
        env_nested_delimiter: str | None = None,
    ) -> None:
        self._environ = dict(environ)
        super().__init__(
            settings_cls,
            case_sensitive=case_sensitive,
            env_prefix=env_prefix,
            env_nested_delimiter=env_nested_delimiter,
        )

    def _load_env_vars(self) -> Mapping[str, str | None]:
        return parse_env_vars(
            self._environ, self.case_sensitive, self.env_ignore_empty, self.env_parse_none_str
        )


def read_secrets_directory(secrets_dir: Path, env_prefix: str) -> dict[str, str]:
    """Read a directory of secret files into an environment-variable style mapping.

    One file per secret, named like the environment variable that would otherwise
    carry it, with or without the prefix: both ``TRADINGSYS_DATABASE__PASSWORD`` and
    ``database__password`` resolve to the same field. A single trailing newline is
    stripped, because most tooling adds one and no secret ends in one.

    Entries beginning with a dot are skipped. Kubernetes projects secrets through a
    ``..data`` symlink into timestamped directories, and reading those would produce
    duplicate or malformed entries.

    Raises:
        ConfigurationError: The directory is missing or a file cannot be read.
    """
    if not secrets_dir.is_dir():
        raise ConfigurationError(f"secrets directory {secrets_dir} is not a directory")
    secrets: dict[str, str] = {}
    for entry in sorted(secrets_dir.iterdir()):
        if entry.name.startswith(".") or not entry.is_file():
            continue
        try:
            content = entry.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(f"secret file {entry} could not be read: {exc}") from exc
        name = entry.name
        if not name.upper().startswith(env_prefix.upper()):
            name = f"{env_prefix}{name}"
        secrets[name.upper()] = content.removesuffix("\n")
    return secrets


@final
class TomlLayeredSource(PydanticBaseSettingsSource):
    """Loads ``base.toml`` and then ``{environment}.toml`` from a config directory.

    The environment file is optional; the base file is not. A missing or malformed
    file raises :class:`~tradingsys.core.errors.ConfigurationError` rather than
    yielding an empty mapping, because silently starting on defaults is how a service
    ends up pointed at the wrong venue.
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        config_dir: Path,
        environment: str,
    ) -> None:
        super().__init__(settings_cls)
        self._config_dir = config_dir
        self._environment = environment
        self._data: dict[str, Any] | None = None

    @property
    def base_path(self) -> Path:
        return self._config_dir / "base.toml"

    @property
    def environment_path(self) -> Path:
        return self._config_dir / f"{self._environment}.toml"

    def _load(self) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if not self._config_dir.is_dir():
            raise ConfigurationError(
                f"configuration directory {self._config_dir} does not exist. Set "
                f"TRADINGSYS_CONFIG_DIR to the directory holding base.toml."
            )
        merged = _read_toml(self.base_path, required=True)
        reject_secrets_from_files(merged, self.settings_cls, str(self.base_path))
        overlay = _read_toml(self.environment_path, required=False)
        if overlay:
            reject_secrets_from_files(overlay, self.settings_cls, str(self.environment_path))
            merged = deep_merge(merged, overlay)
        self._data = merged
        return merged

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:  # noqa: ARG002
        data = self._load()
        if field_name in data:
            return data[field_name], field_name, True
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return dict(self._load())

    def __repr__(self) -> str:
        return f"TomlLayeredSource(dir={self._config_dir}, environment={self._environment})"


def _read_toml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.is_file():
        if required:
            raise ConfigurationError(
                f"required configuration file {path} is missing. It holds the non-secret "
                f"defaults shared by every environment."
            )
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigurationError(f"{path} could not be read: {exc}") from exc


def resolve_config_dir(candidates: Sequence[Path]) -> Path:
    """Return the first candidate directory that exists.

    Raises:
        ConfigurationError: None of the candidates exist.
    """
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    listed = ", ".join(str(candidate) for candidate in candidates)
    raise ConfigurationError(
        f"no configuration directory found. Looked in: {listed}. Set TRADINGSYS_CONFIG_DIR "
        f"to the directory holding base.toml."
    )
