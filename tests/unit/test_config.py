"""Settings.

Small module, but it decides how the gate behaves on a CI runner, and the
reason it refuses unknown keys is that the alternative — silently ignoring a
typo — leaves a job running at defaults with nobody the wiser.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from llmops.config import ExportSettings, GateSettings, LogSettings, Settings, load
from llmops.errors import ConfigError

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _neutral_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No inherited LLMOPS_* variables, and no `.env` in reach."""
    for name in tuple(os.environ):
        if name.startswith("LLMOPS_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


class TestDefaults:
    def test_a_machine_with_no_configuration_gets_working_defaults(self):
        settings = load()
        assert settings.gate.series_budget >= 1
        assert settings.gate.min_samples >= 1
        assert settings.log.format == "json"

    def test_nothing_is_exported_by_default(self):
        # A telemetry tool is exactly the component that should not surprise
        # anyone by sending somewhere.
        assert load().export.otlp_endpoint == ""

    def test_settings_are_frozen(self):
        with pytest.raises(ValidationError):
            load().gate.min_samples = 4


class TestEnvironment:
    def test_a_nested_variable_reaches_its_section(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LLMOPS_GATE__MIN_SAMPLES", "77")
        monkeypatch.setenv("LLMOPS_LOG__FORMAT", "console")

        settings = load()

        assert settings.gate.min_samples == 77
        assert settings.log.format == "console"

    def test_a_dot_env_file_is_read(self, tmp_path: Path):
        (tmp_path / ".env").write_text("LLMOPS_GATE__SERIES_BUDGET=55\n", encoding="utf-8")
        assert load().gate.series_budget == 55

    def test_a_real_variable_beats_the_dot_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        (tmp_path / ".env").write_text("LLMOPS_GATE__SERIES_BUDGET=55\n", encoding="utf-8")
        monkeypatch.setenv("LLMOPS_GATE__SERIES_BUDGET", "99")
        assert load().gate.series_budget == 99

    def test_a_misspelt_field_is_an_error_rather_than_a_shrug(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Without this the typo leaves a CI job at the default and reporting
        # nothing. Reported as a ConfigError rather than pydantic's own error,
        # because the command line renders this tool's errors and lets anything
        # else escape as a traceback.
        monkeypatch.setenv("LLMOPS_GATE__MIN_SAMPLE", "5")
        with pytest.raises(ConfigError, match="min_sample"):
            load()

    def test_a_misspelt_section_is_an_error_too(self, monkeypatch: pytest.MonkeyPatch):
        # The worse of the two typos: pydantic never builds a `logs` key, so
        # extra="forbid" has nothing to reject and the variable is dropped.
        monkeypatch.setenv("LLMOPS_LOGS__LEVEL", "DEBUG")
        with pytest.raises(ConfigError, match="LLMOPS_LOGS__LEVEL"):
            load()

    def test_the_error_names_the_sections_that_do_exist(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("LLMOPS_NETWORK__PROXY", "http://example.invalid")
        with pytest.raises(ConfigError) as caught:
            load()
        for section in ("gate", "log", "export"):
            assert section in caught.value.remedy

    def test_variables_belonging_to_other_tools_are_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
        monkeypatch.setenv("LLMOPSOMETHING", "unprefixed")
        assert load().gate.min_samples >= 1

    def test_an_explicit_environment_can_be_supplied(self):
        with pytest.raises(ConfigError):
            load({"LLMOPS_TYPO__FIELD": "1"})


class TestBounds:
    @pytest.mark.parametrize("value", [0, -1, 1_000_001])
    def test_the_series_budget_is_bounded(self, value: int):
        with pytest.raises(ValidationError):
            GateSettings(series_budget=value)

    @pytest.mark.parametrize("value", [0, -1])
    def test_the_sample_minimum_is_bounded(self, value: int):
        with pytest.raises(ValidationError):
            GateSettings(min_samples=value)

    @pytest.mark.parametrize("value", [0.0, -1.0, 121.0])
    def test_the_export_timeout_is_bounded(self, value: float):
        # A zero timeout means every export fails instantly, which looks like a
        # collector outage rather than a configuration error.
        with pytest.raises(ValidationError):
            ExportSettings(timeout_s=value)

    def test_the_log_level_is_a_closed_set(self):
        with pytest.raises(ValidationError):
            LogSettings(level="TRACE")  # type: ignore[arg-type]

    def test_the_log_format_is_a_closed_set(self):
        with pytest.raises(ValidationError):
            LogSettings(format="xml")  # type: ignore[arg-type]

    def test_settings_can_be_built_directly(self):
        settings = Settings(gate=GateSettings(min_samples=3), log=LogSettings(level="ERROR"))
        assert settings.gate.min_samples == 3
        assert settings.log.level == "ERROR"
