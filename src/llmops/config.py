"""Environment-driven defaults.

Small on purpose. Almost everything about a verdict belongs in the objectives
file, which is committed and reviewed; what lives here is the handful of
settings that differ between a laptop and a CI runner, and none of them change
what the gate decides.

``extra="forbid"`` catches a misspelt *field*. It does not catch a misspelt
*section* — pydantic never builds a ``logs`` key for it to reject — so
:func:`load` checks the section names itself. Both are translated into this
tool's own error type, because the command line renders those and lets anything
else escape as a traceback: a guard whose test does not exercise the boundary
that consumes it is a guard nobody has checked.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from llmops.errors import ConfigError


class Section(BaseModel):
    """Base for the settings sections."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GateSettings(Section):
    """Defaults for the gate."""

    #: Series a metric may produce before the cardinality guard refuses it.
    series_budget: Annotated[int, Field(ge=1, le=1_000_000)] = 1000
    #: Calls a burn rule needs in its long window before it will fire.
    min_samples: Annotated[int, Field(ge=1, le=1_000_000)] = 30


class LogSettings(Section):
    """Where diagnostics go and what they look like.

    Always stderr — that is not configurable, because stdout carries the report.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "console"] = "json"


class ExportSettings(Section):
    """Where telemetry is sent, when it is sent anywhere."""

    #: An OTLP/HTTP endpoint. Empty means nothing is exported, which is the
    #: default: a telemetry tool is exactly the component that should not
    #: surprise anyone by sending somewhere.
    otlp_endpoint: str = ""
    timeout_s: Annotated[float, Field(gt=0, le=120)] = 10.0


class Settings(BaseSettings):
    """The whole configuration.

    Environment variables are ``LLMOPS_<SECTION>__<FIELD>``, for example
    ``LLMOPS_GATE__MIN_SAMPLES=50``.
    """

    model_config = SettingsConfigDict(
        env_prefix="LLMOPS_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        frozen=True,
    )

    gate: GateSettings = GateSettings()
    log: LogSettings = LogSettings()
    export: ExportSettings = ExportSettings()


def _reject_unknown_sections(names: Iterable[str]) -> None:
    """Fail on an ``LLMOPS_`` variable whose section is not a real one.

    Covers the process environment, which is where a CI runner sets things and
    where a mistake is invisible. A misspelt section in a ``.env`` file is not
    covered and does not need to be: that file is in the repository, next to the
    person editing it.
    """
    prefix = "LLMOPS_"
    known = set(Settings.model_fields)
    unknown = sorted(
        name
        for name in names
        if name.startswith(prefix) and name[len(prefix) :].split("__", 1)[0].lower() not in known
    )
    if unknown:
        raise ConfigError(
            f"unknown setting(s): {', '.join(unknown)}.",
            remedy=(
                f"Sections are {', '.join(sorted(known))}. "
                f"Variables are {prefix}<SECTION>__<FIELD>, for example "
                f"{prefix}GATE__MIN_SAMPLES=50."
            ),
        )


def load(environ: Mapping[str, str] | None = None) -> Settings:
    """Read settings from the environment and ``.env``."""
    _reject_unknown_sections(os.environ if environ is None else environ)
    try:
        return Settings()
    except ValidationError as exc:
        fields = ", ".join(".".join(str(part) for part in error["loc"]) for error in exc.errors())
        raise ConfigError(
            f"the settings are invalid: {fields or 'see below'}.",
            remedy=(
                "Variables are LLMOPS_<SECTION>__<FIELD>, for example "
                f"LLMOPS_GATE__MIN_SAMPLES=50. Unknown names are refused rather "
                f"than ignored.\n  {exc}"
            ),
        ) from exc
