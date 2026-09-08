"""Objectives: what "good" means, declared in a file that is reviewed.

Three kinds, because they are the three questions an LLM platform is actually
asked, and each one counts a different thing as *bad*:

``availability``  a call that did not succeed. See ``Span.succeeded`` for why a
                  refusal is not a failure.
``latency``       a call slower than a threshold. Note that this is a *count* of
                  slow calls, not a percentile: an objective phrased as "99% of
                  calls under 2s" has an error budget of the 1% that were not,
                  and that is the same arithmetic as availability. Phrasing it
                  as "p99 under 2s" instead would need a different and much
                  worse estimator over a sliding window.
``spend``         money, over a budget period. Burn rate applies unchanged — the
                  budget is dollars rather than failed calls — which is why this
                  is one engine and not two.

Selectors narrow an objective to part of the traffic. They match on the span's
own fields and on its attributes, with exact string equality only: no globs, no
regular expressions, no expressions of any kind. A selector language is a place
for a catastrophically backtracking pattern to hide, and an objective file is
read by a CI job from a repository.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml

from llmops.errors import ObjectiveError
from llmops.slo.burnrate import DEFAULT_MIN_SAMPLES, STANDARD_RULES, BurnRule
from llmops.telemetry.span import Span

#: Objective files are small and committed.
MAX_OBJECTIVES_BYTES = 1024 * 1024
#: A guard against an objective file that is really a dataset.
MAX_OBJECTIVES = 64

Kind = Literal["availability", "latency", "spend"]

#: Selector fields that live on the span itself. Anything else is looked up in
#: the attribute bag.
SPAN_FIELDS = frozenset({"provider", "model", "operation", "outcome"})


@dataclass(frozen=True, slots=True)
class Selector:
    """Which spans an objective applies to."""

    match: dict[str, str] = field(default_factory=dict)

    def matches(self, span: Span) -> bool:
        """Whether *span* is in scope. Exact equality, every key, or nothing."""
        for key, expected in self.match.items():
            actual = getattr(span, key) if key in SPAN_FIELDS else span.attributes.get(key)
            if actual != expected:
                return False
        return True

    def describe(self) -> str:
        """Return a short human form for a report."""
        if not self.match:
            return "all traffic"
        return ", ".join(f"{key}={value}" for key, value in sorted(self.match.items()))


@dataclass(frozen=True, slots=True)
class Objective:
    """One declared target."""

    name: str
    kind: Kind
    #: 0 < target < 1 for availability and latency: the fraction that must be
    #: good. Unused for spend, which uses ``budget_usd``.
    target: float = 0.0
    #: The period the budget is sized over.
    period: timedelta = timedelta(days=30)
    selector: Selector = field(default_factory=Selector)
    rules: tuple[BurnRule, ...] = STANDARD_RULES
    #: Latency only: a call slower than this is bad.
    threshold_ms: float = 0.0
    #: Spend only: dollars for the whole period.
    budget_usd: Decimal = Decimal(0)
    description: str = ""

    @property
    def error_budget(self) -> float:
        """The fraction of events that may be bad.

        For spend this is 1.0: the whole budget may be spent over the period,
        and burn rate measures how fast against that. The failure "rate" for a
        spend objective is the fraction of the period's budget spent per unit of
        the period, which puts it on the same scale as the other two.
        """
        if self.kind == "spend":
            return 1.0
        return 1.0 - self.target

    def is_bad(self, span: Span) -> bool:
        """Whether *span* counts against the budget.

        Not defined for spend, which measures dollars rather than events; the
        evaluator never calls it there.
        """
        if self.kind == "availability":
            return not span.succeeded
        if self.kind == "latency":
            # A failed call is not a slow call. Counting it as one would mean an
            # outage silently consumes the latency budget too, and the two
            # objectives would stop being independent measurements.
            return span.succeeded and span.duration_ms > self.threshold_ms
        return False  # pragma: no cover - spend never asks

    def digest(self) -> str:
        """Return a content address over what this objective measures."""
        material = "␟".join(
            [
                self.name,
                self.kind,
                f"{self.target:.10f}",
                f"{self.period.total_seconds():.0f}",
                f"{self.threshold_ms:.3f}",
                str(self.budget_usd),
                self.selector.describe(),
                "|".join(
                    f"{rule.name}:{rule.burn_rate:g}:{rule.long_window.total_seconds():.0f}"
                    f":{rule.short.total_seconds():.0f}:{rule.min_samples}"
                    for rule in self.rules
                ),
            ]
        )
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "period_s": self.period.total_seconds(),
            "selector": self.selector.describe(),
            "error_budget": round(self.error_budget, 10),
        }
        if self.kind in {"availability", "latency"}:
            payload["target"] = self.target
        if self.kind == "latency":
            payload["threshold_ms"] = self.threshold_ms
        if self.kind == "spend":
            payload["budget_usd"] = float(self.budget_usd)
        return payload


@dataclass(frozen=True, slots=True)
class ObjectiveSet:
    """Every objective in one file."""

    name: str
    objectives: tuple[Objective, ...]
    source: str = "<memory>"

    def __len__(self) -> int:
        return len(self.objectives)

    def digest(self) -> str:
        """Return a content address over every objective, order-independent."""
        material = "\n".join(sorted(objective.digest() for objective in self.objectives))
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def load_objectives(path: str | Path) -> ObjectiveSet:
    """Read objectives from YAML."""
    file = Path(path)
    if not file.is_file():
        raise ObjectiveError(
            f"the objectives file {str(file)!r} could not be opened.",
            remedy="Check the path. An example ships in examples/objectives.yaml.",
        )
    size = file.stat().st_size
    if size > MAX_OBJECTIVES_BYTES:
        raise ObjectiveError(
            f"the objectives file is {size} bytes; the limit is {MAX_OBJECTIVES_BYTES}.",
            remedy="Objectives are declarations, not data.",
        )
    return parse_objectives(file.read_text(encoding="utf-8"), source=str(file))


def parse_objectives(text: str, *, source: str = "<string>") -> ObjectiveSet:
    """Parse objectives. ``yaml.safe_load`` only."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ObjectiveError(
            f"the objectives are not valid YAML: {exc}.",
            remedy="Check indentation and quoting around the failing line.",
        ) from exc
    if not isinstance(raw, dict):
        raise ObjectiveError(
            "an objectives file is a mapping with an 'objectives' list.",
            remedy="See examples/objectives.yaml.",
        )

    entries = raw.get("objectives")
    if not isinstance(entries, list) or not entries:
        raise ObjectiveError(
            "the file declares no objectives.",
            remedy="A gate with nothing to check would pass unconditionally.",
        )
    if len(entries) > MAX_OBJECTIVES:
        raise ObjectiveError(
            f"{len(entries)} objectives; the limit is {MAX_OBJECTIVES}.",
            remedy="Raise MAX_OBJECTIVES deliberately if a platform really has this many.",
        )

    objectives = tuple(_objective(entry, index=index) for index, entry in enumerate(entries))
    seen: set[str] = set()
    for objective in objectives:
        if objective.name in seen:
            raise ObjectiveError(
                f"two objectives are named {objective.name!r}.",
                remedy="Names identify findings in the report, so they have to be unique.",
            )
        seen.add(objective.name)

    return ObjectiveSet(
        name=str(raw.get("name") or file_stem(source)),
        objectives=objectives,
        source=source,
    )


def file_stem(source: str) -> str:
    """Return a readable name derived from a path, for a file that declares none."""
    return Path(source).stem or "objectives"


_KNOWN_KEYS = frozenset(
    {
        "name",
        "kind",
        "target",
        "period",
        "select",
        "threshold_ms",
        "budget_usd",
        "description",
        "rules",
        "min_samples",
    }
)


def _objective(entry: Any, *, index: int) -> Objective:
    where = f"objectives[{index}]"
    if not isinstance(entry, dict):
        raise ObjectiveError(f"{where} is not a mapping.", remedy="Each objective is a mapping.")

    unknown = sorted(set(entry) - _KNOWN_KEYS)
    if unknown:
        # Refused rather than ignored. A misspelt `threshold_ms` on a latency
        # objective would otherwise leave the threshold at zero, making every
        # call slow and every window an outage.
        raise ObjectiveError(
            f"{where} has unknown key(s): {', '.join(unknown)}.",
            remedy=f"Known keys are {', '.join(sorted(_KNOWN_KEYS))}.",
        )

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ObjectiveError(f"{where} has no name.", remedy="Findings are reported by name.")

    kind = entry.get("kind", "availability")
    if kind not in {"availability", "latency", "spend"}:
        raise ObjectiveError(
            f"{where} has an unknown kind {kind!r}.",
            remedy="Known kinds are availability, latency, spend.",
        )

    period = _period(entry.get("period", "30d"), where=where)
    rules = _rules(entry, where=where)

    target = 0.0
    threshold_ms = 0.0
    budget = Decimal(0)

    if kind in {"availability", "latency"}:
        target = _target(entry.get("target"), where=where)
    if kind == "latency":
        threshold_ms = _threshold(entry.get("threshold_ms"), where=where)
    if kind == "spend":
        budget = _budget(entry.get("budget_usd"), where=where)

    return Objective(
        name=name.strip(),
        kind=kind,
        target=target,
        period=period,
        selector=Selector(_select(entry.get("select"), where=where)),
        rules=rules,
        threshold_ms=threshold_ms,
        budget_usd=budget,
        description=str(entry.get("description") or ""),
    )


def _target(value: Any, *, where: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ObjectiveError(
            f"{where} has no numeric target.",
            remedy="A target is a fraction, for example 0.999 for three nines.",
        )
    target = float(value)
    if not 0.0 < target < 1.0:
        # 1.0 is refused explicitly: a 100% objective has a zero error budget,
        # every burn rate is a division by zero, and the only honest report is
        # that one failure has consumed everything. Saying so at parse time is
        # kinder than saying it in an incident.
        raise ObjectiveError(
            f"{where} has target {target}, which must be between 0 and 1 exclusive.",
            remedy=(
                "A target of 1.0 means a zero error budget: every failure is a total "
                "burn and no rate is meaningful. Use 0.9999 if that is what you mean."
            ),
        )
    return target


def _threshold(value: Any, *, where: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ObjectiveError(
            f"{where} is a latency objective with no positive threshold_ms.",
            remedy="Without it every call is slow and every window is an outage.",
        )
    return float(value)


def _budget(value: Any, *, where: str) -> Decimal:
    try:
        budget = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ObjectiveError(
            f"{where} has a non-numeric budget_usd: {value!r}.",
            remedy="A spend objective needs a budget in dollars for the period.",
        ) from exc
    if budget <= 0:
        raise ObjectiveError(
            f"{where} has a budget of {budget}.",
            remedy="A zero budget means any spend at all is a total burn.",
        )
    return budget


def _select(value: Any, *, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ObjectiveError(
            f"{where} has a 'select' that is not a mapping.",
            remedy="A selector is field: value pairs, matched with exact equality.",
        )
    selectors: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ObjectiveError(
                f"{where} has a non-string selector {key!r}.",
                remedy="Selectors are exact string matches; there is no pattern syntax.",
            )
        selectors[key] = item
    return selectors


def _period(value: Any, *, where: str) -> timedelta:
    period = _duration(value, where=where, key="period")
    if period <= timedelta(0):
        raise ObjectiveError(f"{where} has a non-positive period.", remedy="Use e.g. '30d'.")
    return period


def _rules(entry: dict[str, Any], *, where: str) -> tuple[BurnRule, ...]:
    minimum = entry.get("min_samples")
    declared = entry.get("rules")

    if declared is None:
        if minimum is None:
            return STANDARD_RULES
        floor = _min_samples(minimum, where=where)
        # Applying a file-level minimum to the standard table, rather than
        # making the caller restate all four rules to change one number.
        return tuple(
            BurnRule(
                name=rule.name,
                severity=rule.severity,
                long_window=rule.long_window,
                short_window=rule.short,
                burn_rate=rule.burn_rate,
                min_samples=floor,
            )
            for rule in STANDARD_RULES
        )

    if not isinstance(declared, list) or not declared:
        raise ObjectiveError(
            f"{where} declares 'rules' but not as a non-empty list.",
            remedy="Omit 'rules' to use the conventional four-rule table.",
        )

    default_minimum = None if minimum is None else _min_samples(minimum, where=where)
    return tuple(
        _rule(item, where=f"{where}.rules[{index}]", default_minimum=default_minimum)
        for index, item in enumerate(declared)
    )


def _rule(entry: Any, *, where: str, default_minimum: int | None) -> BurnRule:
    if not isinstance(entry, dict):
        raise ObjectiveError(f"{where} is not a mapping.", remedy="Each rule is a mapping.")
    known = {"name", "severity", "long_window", "short_window", "burn_rate", "min_samples"}
    unknown = sorted(set(entry) - known)
    if unknown:
        raise ObjectiveError(
            f"{where} has unknown key(s): {', '.join(unknown)}.",
            remedy=f"Known keys are {', '.join(sorted(known))}.",
        )

    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ObjectiveError(f"{where} has no name.", remedy="Rules are reported by name.")

    severity = entry.get("severity", "page")
    if severity not in {"page", "ticket"}:
        raise ObjectiveError(
            f"{where} has an unknown severity {severity!r}.",
            remedy="Known severities are page and ticket.",
        )

    burn = entry.get("burn_rate")
    if not isinstance(burn, int | float) or isinstance(burn, bool) or burn <= 0:
        raise ObjectiveError(
            f"{where} has no positive burn_rate.",
            remedy="A burn rate is a multiple of the error budget, for example 14.4.",
        )

    long_window = _duration(entry.get("long_window"), where=where, key="long_window")
    if long_window <= timedelta(0):
        raise ObjectiveError(f"{where} has a non-positive long_window.", remedy="Use e.g. '1h'.")

    short_raw = entry.get("short_window")
    short_window = (
        None if short_raw is None else _duration(short_raw, where=where, key="short_window")
    )
    if short_window is not None and not timedelta(0) < short_window <= long_window:
        raise ObjectiveError(
            f"{where} has a short_window that is not inside its long_window.",
            remedy="The short window measures whether the burn is still happening.",
        )

    minimum = entry.get("min_samples")
    resolved = (
        _min_samples(minimum, where=where)
        if minimum is not None
        else (default_minimum if default_minimum is not None else DEFAULT_MIN_SAMPLES)
    )

    return BurnRule(
        name=name.strip(),
        severity=severity,
        long_window=long_window,
        short_window=short_window,
        burn_rate=float(burn),
        min_samples=resolved,
    )


def _min_samples(value: Any, *, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ObjectiveError(
            f"{where} has a min_samples that is not a positive integer.",
            remedy="Below this many calls a rule reports 'not evaluated' rather than 'ok'.",
        )
    return value


#: Suffixes a duration may carry. Seconds are included for tests and for the
#: rare objective over a very short window; days are the longest unit, because
#: a month is not a fixed length and an objective that silently means 30 days on
#: one file and 31 on another is a bug waiting for February.
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

#: The shortest a duration string can be: one digit and one unit.
_SHORTEST_DURATION = 2


def _duration(value: Any, *, where: str, key: str) -> timedelta:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return timedelta(seconds=float(value))
    if not isinstance(value, str) or len(value) < _SHORTEST_DURATION:
        raise ObjectiveError(
            f"{where} has an unusable {key}: {value!r}.",
            remedy="Durations are like '5m', '6h' or '30d'; a bare number is seconds.",
        )
    unit = value[-1]
    if unit not in _UNITS:
        raise ObjectiveError(
            f"{where} has an unknown unit in {key}: {value!r}.",
            remedy=f"Known units are {', '.join(sorted(_UNITS))}.",
        )
    try:
        amount = float(value[:-1])
    except ValueError as exc:
        raise ObjectiveError(
            f"{where} has an unparseable {key}: {value!r}.",
            remedy="Durations are like '5m', '6h' or '30d'.",
        ) from exc
    return timedelta(seconds=amount * _UNITS[unit])
