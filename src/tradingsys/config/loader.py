"""Startup configuration loading.

One entry point, :func:`load_settings`, which either returns a fully validated
:class:`~tradingsys.config.settings.Settings` or raises
:class:`~tradingsys.core.errors.ConfigurationError` with a report naming every field
that is wrong. There is no partial success and no fallback to defaults: a trading
process that starts with the wrong account, the wrong database, or the wrong venue
environment is worse than one that does not start.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from tradingsys.config.settings import (
    ENV_NESTED_DELIMITER,
    ENV_PREFIX,
    Environment,
    Settings,
    SettingsContext,
    settings_context,
)
from tradingsys.core.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["format_validation_error", "load_settings", "missing_variables"]


def load_settings(
    *,
    config_dir: Path | None = None,
    environment: Environment | None = None,
    secrets_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load and validate the configuration for this process.

    Args:
        config_dir: Directory holding ``base.toml`` and the per-environment files.
            Defaults to ``TRADINGSYS_CONFIG_DIR``, then ``./config``.
        environment: Which environment file to layer on top of the base. Defaults to
            ``TRADINGSYS_APP__ENVIRONMENT``, then ``development``.
        secrets_dir: Directory into which a secret manager projects one file per
            secret. Defaults to ``TRADINGSYS_SECRETS_DIR``, then unused.
        environ: Environment mapping to resolve defaults from. Defaults to the real
            process environment.

    Raises:
        ConfigurationError: Any file is missing or malformed, any secret appears in a
            config file, or any value fails validation.
    """
    resolved = SettingsContext.from_environ(environ)
    context = SettingsContext(
        config_dir=Path(config_dir) if config_dir is not None else resolved.config_dir,
        environment=environment if environment is not None else resolved.environment,
        secrets_dir=Path(secrets_dir) if secrets_dir is not None else resolved.secrets_dir,
        environ=resolved.environ,
    )
    if context.secrets_dir is not None and not context.secrets_dir.is_dir():
        raise ConfigurationError(
            f"secrets directory {context.secrets_dir} does not exist. Unset "
            f"{ENV_PREFIX}SECRETS_DIR or mount the directory the secret manager writes to."
        )

    with settings_context(context):
        try:
            return Settings()
        except ValidationError as exc:
            raise ConfigurationError(format_validation_error(exc, context)) from exc


def format_validation_error(error: ValidationError, context: SettingsContext) -> str:
    """Render a pydantic validation failure as an operator readable report.

    Each line names the dotted field path, the problem, and the environment variable
    that would set it, because the most common cause of a startup failure is a value
    nobody knows how to supply.
    """
    lines = [
        f"configuration is invalid for the {context.environment.value} environment "
        f"(config dir: {context.config_dir})",
    ]
    for detail in error.errors():
        path = ".".join(str(part) for part in detail["loc"])
        lines.append(f"  {path}: {detail['msg']} {_hint_for(detail, path)}")
    lines.append(
        "Non-secret values belong in the TOML files; credentials belong in environment "
        "variables or the secrets directory."
    )
    return "\n".join(lines)


def _hint_for(detail: Mapping[str, Any], dotted_path: str) -> str:
    """The remedy for one validation error.

    Telling an operator to set a variable that is already set, which is what a single
    generic hint does when the real problem is an unrecognised name, sends them looking
    for the wrong thing. The advice follows the error type.
    """
    if detail.get("type") == "extra_forbidden":
        return (
            f"[unrecognised setting: either remove {env_var_for(dotted_path)} from the "
            f"environment, or add the field to the configuration model]"
        )
    return f"[set with {env_var_for(dotted_path)}]"


def missing_variables(error: ValidationError) -> tuple[str, ...]:
    """Environment variables for the fields that are required and absent.

    Distinguished from fields that are present but unrecognised, which need the
    opposite remedy.
    """
    return tuple(
        env_var_for(".".join(str(part) for part in detail["loc"]))
        for detail in error.errors()
        if detail.get("type") == "missing"
    )


def env_var_for(dotted_path: str) -> str:
    """The environment variable that supplies a given dotted configuration path."""
    return ENV_PREFIX + ENV_NESTED_DELIMITER.join(part.upper() for part in dotted_path.split("."))
