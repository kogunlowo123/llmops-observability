"""Spans and windows: the record of what happened, and the file it lives in."""

from __future__ import annotations

from llmops.telemetry.span import Operation, Outcome, Span
from llmops.telemetry.window import Window, build_window, read_window

__all__ = ["Operation", "Outcome", "Span", "Window", "build_window", "read_window"]
