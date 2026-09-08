"""Refusing a metric whose labels would explode a time-series database."""

from __future__ import annotations

from llmops.cardinality.budget import (
    DEFAULT_LABELS,
    DEFAULT_SERIES_BUDGET,
    KNOWN_UNBOUNDED,
    Label,
    LabelRegistry,
    MetricSpec,
    bounded,
    unbounded,
)

__all__ = [
    "DEFAULT_LABELS",
    "DEFAULT_SERIES_BUDGET",
    "KNOWN_UNBOUNDED",
    "Label",
    "LabelRegistry",
    "MetricSpec",
    "bounded",
    "unbounded",
]
