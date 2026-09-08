"""The three report shapes: JSON for a script, JUnit for CI, Markdown for a person."""

from __future__ import annotations

from llmops.report import json_report, junit, markdown

__all__ = ["json_report", "junit", "markdown"]
