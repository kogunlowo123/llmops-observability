"""The command line.

Six commands:

``gate``     evaluate objectives over a window and exit non-zero. The CI command.
``cost``     what a window cost, reconciled against the provider's own numbers
``synth``    generate a deterministic telemetry window
``check``    verify a committed window still matches what the generator produces
``labels``   ask whether a metric's labels would explode the series count
``doctor``   what this installation would do, without needing a window

Two conventions hold everywhere:

**The report goes to stdout and everything else to stderr.** A build script pipes
stdout into a parser; a log line in the middle of that is a broken build with a
confusing cause.

**Exit codes distinguish "an objective was missed" (2) from "the tool could not
run" (3).** They call for different responses — one is a service that got worse,
the other is a broken pipeline — and collapsing them makes a CI job unable to
tell a real incident from its own misconfiguration.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from llmops import __version__
from llmops.cardinality.budget import DEFAULT_LABELS, LabelRegistry, bounded
from llmops.config import Settings, load
from llmops.corpus.synth import SCENARIOS, Scenario, generate
from llmops.cost.pricebook import load_pricebook
from llmops.cost.report import CostReport, cost_window, unpriced_models
from llmops.errors import (
    EXIT_BUDGET_BURNED,
    EXIT_ERROR,
    EXIT_OK,
    LlmopsError,
    ObjectiveError,
)
from llmops.logging import configure
from llmops.report import json_report, junit, markdown
from llmops.slo.evaluate import Evaluation, evaluate
from llmops.slo.objective import load_objectives, parse_objectives
from llmops.telemetry.export import build_exporter
from llmops.telemetry.window import Window, read_window

#: Objectives used by ``doctor``: small, self-contained, and holding by
#: construction against a generated window, so the command proves the pipeline
#: works on a machine with no telemetry and no credentials.
DOCTOR_OBJECTIVES = """
name: doctor
objectives:
  - name: availability
    kind: availability
    target: 0.99
    period: 30d
    min_samples: 10
    rules:
      - name: fast-burn
        severity: page
        long_window: 1h
        short_window: 5m
        burn_rate: 14.4
"""


def _use_utf8(stream: Any) -> None:
    """Force UTF-8 on a standard stream.

    Without this the tool crashes on a Windows console, whose default encoding
    is cp1252: the report is JSON, an objective's name and an error type are
    arbitrary Unicode, and a single '≥' raises ``UnicodeEncodeError`` after the
    whole evaluation has already been done. Found in the previous repository in
    this series by running the CLI as a real process; a library test cannot see
    it, because pytest's capture replaces the stream.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:  # pragma: no cover - not a real console under capture
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):  # pragma: no cover - a stream that refuses
        return


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than raising."""
    _use_utf8(sys.stdout)
    _use_utf8(sys.stderr)

    parser = build_parser()
    args = parser.parse_args(argv)

    # Inside the try, not above it. Reading the settings is the first thing that
    # can fail on a badly configured CI runner, and a failure there must be
    # rendered like every other one rather than escaping as a traceback.
    try:
        settings = load()
        configure(
            level=args.log_level or settings.log.level,
            json_output=settings.log.format == "json",
        )
        return int(args.handler(args, settings))
    except LlmopsError as exc:
        print(exc.render(), file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


# -- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="llmops",
        description="LLM telemetry you can gate on.",
    )
    parser.add_argument("--version", action="version", version=f"llmops {__version__}")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="override LLMOPS_LOG__LEVEL for this invocation",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gate = sub.add_parser("gate", help="evaluate objectives over a window; exit 2 when one breaks")
    _add_window_arguments(gate)
    gate.add_argument("--objectives", required=True, type=Path, help="path to the objectives file")
    gate.add_argument("--pricebook", type=Path, default=None, help="required by a spend objective")
    gate.add_argument(
        "--at",
        default="",
        help=(
            "evaluate as at this instant (ISO 8601) instead of the window's end. "
            "The rules look backwards from it."
        ),
    )
    gate.add_argument(
        "--with-cost",
        action="store_true",
        help="include a spend breakdown in the report; needs --pricebook",
    )
    _add_output_arguments(gate)
    gate.set_defaults(handler=_command_gate)

    cost = sub.add_parser("cost", help="what a window cost, reconciled against the provider")
    _add_window_arguments(cost)
    cost.add_argument("--pricebook", required=True, type=Path, help="path to the price book")
    cost.add_argument(
        "--by",
        choices=["model", "provider", "operation", "outcome"],
        default="model",
        help="how to group the breakdown (default: model)",
    )
    cost.add_argument(
        "--fail-over",
        type=float,
        default=None,
        help="exit 2 when the window cost more than this many dollars",
    )
    cost.add_argument("--json-out", type=Path, default=None, help="write the JSON report here")
    cost.set_defaults(handler=_command_cost)

    synth = sub.add_parser("synth", help="generate a deterministic telemetry window")
    synth.add_argument(
        "--scenario",
        choices=sorted(SCENARIOS),
        default="steady",
        help="which shipped scenario to generate (default: steady)",
    )
    synth.add_argument("--out", required=True, type=Path, help="where to write the window")
    synth.add_argument("--seed", type=int, default=None, help="override the scenario's seed")
    synth.add_argument(
        "--export",
        choices=["jsonl", "otlp"],
        default="jsonl",
        help="where to send it (default: jsonl)",
    )
    synth.add_argument("--endpoint", default="", help="OTLP endpoint, with --export otlp")
    synth.add_argument(
        "--allow-network",
        action="store_true",
        help=(
            "permit an exporter that opens a connection. Off by default: a telemetry "
            "tool is the component that should not surprise anyone by sending somewhere."
        ),
    )
    synth.set_defaults(handler=_command_synth)

    check = sub.add_parser(
        "check", help="does a committed window still match what the generator produces?"
    )
    check.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    check.add_argument("--window", required=True, type=Path, help="the committed window")
    check.set_defaults(handler=_command_check)

    labels = sub.add_parser("labels", help="would this metric's labels explode the series count?")
    labels.add_argument("--metric", required=True, help="the metric name")
    labels.add_argument(
        "--label", dest="labels", action="append", default=[], help="a label; repeatable"
    )
    labels.add_argument(
        "--bound",
        dest="bounds",
        action="append",
        default=[],
        metavar="NAME=COUNT",
        help="declare a label bounded at COUNT distinct values; repeatable",
    )
    labels.add_argument("--series-budget", type=int, default=None, help="override the budget")
    labels.set_defaults(handler=_command_labels)

    doctor = sub.add_parser("doctor", help="what this installation would do")
    doctor.add_argument("--window", type=Path, default=None, help="also inspect this window")
    doctor.set_defaults(handler=_command_doctor)

    return parser


def _add_window_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--window", required=True, type=Path, help="path to the telemetry window")


def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json-out", type=Path, default=None, help="write the JSON report here")
    parser.add_argument("--junit-out", type=Path, default=None, help="write JUnit XML here")
    parser.add_argument("--markdown-out", type=Path, default=None, help="write a summary here")


# -- commands ------------------------------------------------------------


def _command_gate(args: argparse.Namespace, _settings: Settings) -> int:
    window = read_window(args.window)
    objectives = load_objectives(args.objectives)
    pricebook = load_pricebook(args.pricebook) if args.pricebook else None

    evaluation = evaluate(window, objectives, pricebook=pricebook, now=_instant(args.at, window))

    cost: CostReport | None = None
    if args.with_cost:
        if pricebook is None:
            raise ObjectiveError(
                "--with-cost needs a price book.",
                remedy="Pass --pricebook, or drop --with-cost.",
            )
        cost = cost_window(window, pricebook)

    _emit(evaluation, args, cost=cost)
    return EXIT_OK if evaluation.passed else EXIT_BUDGET_BURNED


def _command_cost(args: argparse.Namespace, _settings: Settings) -> int:
    window = read_window(args.window)
    pricebook = load_pricebook(args.pricebook)

    missing = unpriced_models(window, pricebook)
    if missing:
        # Named all at once. Raising on the first is right when costing, and
        # wrong here: being told about one missing model at a time is four round
        # trips instead of one.
        raise ObjectiveError(
            f"the window contains {len(missing)} unpriced model(s): {', '.join(missing)}.",
            remedy=(
                f"Add them to {args.pricebook}. An unpriced model is refused rather than "
                "costed at zero, because the one missing from a price book is usually "
                "the newest and most expensive."
            ),
        )

    report = cost_window(window, pricebook, dimension=args.by)
    document = json.dumps(report.as_dict(), indent=2, ensure_ascii=False)
    if args.json_out:
        _write(args.json_out, document)
    else:
        print(document)

    print(f"\n{report.summary()}", file=sys.stderr)
    if report.discrepancies:
        print(
            f"  {len(report.discrepancies)} span(s) disagree with the provider's own cost",
            file=sys.stderr,
        )

    if args.fail_over is not None and report.total_usd > Decimal(str(args.fail_over)):
        print(
            f"FAIL  the window cost ${float(report.total_usd):.6f}, over the "
            f"${args.fail_over:.6f} ceiling.",
            file=sys.stderr,
        )
        return EXIT_BUDGET_BURNED
    return EXIT_OK


def _command_synth(args: argparse.Namespace, settings: Settings) -> int:
    scenario = _scenario(args.scenario, seed=args.seed)
    window = generate(scenario)

    exporter = build_exporter(
        args.export,
        offline=not args.allow_network,
        path=args.out,
        endpoint=args.endpoint or settings.export.otlp_endpoint,
        timeout_s=settings.export.timeout_s,
    )
    redaction = exporter.export(window)
    exporter.close()

    print(
        f"generated {window.total} span(s) over {_human(window.duration)} "
        f"from scenario {scenario.name!r} (seed {scenario.seed})",
        file=sys.stderr,
    )
    print(
        json.dumps(
            {
                "scenario": scenario.name,
                "seed": scenario.seed,
                "spans": window.total,
                "digest": window.digest(),
                "out": str(args.out),
                "redaction": redaction.as_dict(),
            }
        )
    )
    return EXIT_OK


def _command_check(args: argparse.Namespace, _settings: Settings) -> int:
    """Compare a committed window against a freshly generated one.

    Compares digests, not files. A window header carries the span count and the
    digest and would differ on any reformatting; more to the point, a check that
    regenerates a file and diffs it fails forever the moment any field in it is
    a timestamp. What matters is whether the spans still bill and measure the
    same, which is exactly what the digest covers.
    """
    committed = read_window(args.window)
    expected = generate(_scenario(args.scenario, seed=None))

    if committed.digest() == expected.digest():
        print(
            f"{args.window} matches scenario {args.scenario!r}: "
            f"{committed.total} span(s), {committed.digest()}"
        )
        return EXIT_OK

    print(
        f"{args.window} no longer matches scenario {args.scenario!r}.",
        file=sys.stderr,
    )
    print(
        f"  committed: {committed.total} span(s), {committed.digest()}\n"
        f"  generated: {expected.total} span(s), {expected.digest()}",
        file=sys.stderr,
    )
    print(
        f"  Regenerate with: llmops synth --scenario {args.scenario} --out {args.window}",
        file=sys.stderr,
    )
    return EXIT_ERROR


def _command_labels(args: argparse.Namespace, settings: Settings) -> int:
    registry = LabelRegistry(DEFAULT_LABELS)
    for declaration in args.bounds:
        name, _, count = declaration.partition("=")
        if not name or not count.isdigit():
            raise ObjectiveError(
                f"--bound expects NAME=COUNT, got {declaration!r}.",
                remedy="For example: --bound tenant_tier=4",
            )
        registry.register(bounded(name, int(count)))

    budget = args.series_budget if args.series_budget is not None else settings.gate.series_budget
    spec = registry.check(args.metric, args.labels, series_budget=budget)
    print(json.dumps({"accepted": True, **spec.as_dict(), "series_budget": budget}, indent=2))
    print(
        f"\n{spec.name}: {spec.series} series from {len(spec.labels)} label(s), budget {budget}",
        file=sys.stderr,
    )
    return EXIT_OK


def _command_doctor(args: argparse.Namespace, settings: Settings) -> int:
    lines: list[str] = [f"llmops {__version__}", ""]
    lines.append(f"scenarios       {', '.join(sorted(SCENARIOS))}")
    lines.append("exporters       jsonl, otlp")
    lines.append("labels          " + ", ".join(label.name for label in DEFAULT_LABELS))
    lines.append("")
    lines.append(f"series budget   {settings.gate.series_budget}")
    lines.append(f"min samples     {settings.gate.min_samples}")
    lines.append(f"otlp endpoint   {settings.export.otlp_endpoint or '(none; offline)'}")
    lines.append(f"log             {settings.log.level} as {settings.log.format}, to stderr")
    lines.append("")

    # The self-check: generate a short window and gate it. Proves the whole
    # pipeline works on a machine with no telemetry, no collector and no
    # credential.
    scenario = Scenario(
        name="doctor",
        duration=timedelta(hours=6),
        peak_calls_per_hour=120,
        metadata={"shows": "the self-check"},
    )
    window = generate(scenario)
    objectives = parse_objectives(DOCTOR_OBJECTIVES, source="<doctor>")
    evaluation = evaluate(window, objectives)
    verdict = "ok" if evaluation.passed else "FAILED"
    lines.append(
        f"self-check      {verdict}: {window.total} generated span(s), "
        f"{len(evaluation.results)} objective(s) evaluated"
    )

    if args.window:
        inspected = read_window(args.window)
        lines.append("")
        lines.append(f"window          {args.window}")
        lines.append(
            f"interval        {inspected.start.isoformat()} .. {inspected.end.isoformat()}"
        )
        lines.append(f"spans           {inspected.total} ({inspected.failed} not successful)")
        lines.append(f"unreadable      {len(inspected.unreadable)} line(s)")
        lines.append(f"digest          {inspected.digest()}")
        lines.append("models          " + ", ".join(f"{p}/{m}" for p, m in inspected.models()))

    print("\n".join(lines))
    return EXIT_OK if evaluation.passed else EXIT_ERROR


# -- shared --------------------------------------------------------------


def _scenario(name: str, *, seed: int | None) -> Scenario:
    scenario = SCENARIOS[name]
    if seed is None:
        return scenario
    return Scenario(
        name=scenario.name,
        duration=scenario.duration,
        peak_calls_per_hour=scenario.peak_calls_per_hour,
        start=scenario.start,
        models=scenario.models,
        incidents=scenario.incidents,
        environments=scenario.environments,
        routes=scenario.routes,
        seed=seed,
        metadata=dict(scenario.metadata),
    )


def _instant(raw: str, window: Window) -> datetime:
    if not raw:
        return window.end
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ObjectiveError(
            f"--at is not an ISO 8601 instant: {raw!r}.",
            remedy="For example: --at 2026-09-01T22:40:00+00:00",
        ) from exc
    if parsed.tzinfo is None:
        raise ObjectiveError(
            f"--at has no timezone: {raw!r}.",
            remedy="A naive instant means a different moment on every machine. Add an offset.",
        )
    return parsed.astimezone(UTC)


def _emit(
    evaluation: Evaluation, args: argparse.Namespace, *, cost: CostReport | None = None
) -> None:
    document = json_report.render(evaluation, cost=cost)
    if args.json_out:
        _write(args.json_out, document)
    else:
        print(document)

    if args.junit_out:
        _write(args.junit_out, junit.render(evaluation))
    if args.markdown_out:
        _write(args.markdown_out, markdown.render(evaluation, cost=cost))

    print(f"\n{evaluation.summary()}", file=sys.stderr)
    for result in evaluation.breached:
        for rule in result.firing:
            print(
                f"  {rule.rule.severity.upper()}  {result.objective.name}: {rule.summary()}",
                file=sys.stderr,
            )
    for result in evaluation.results:
        for rule in result.results:
            if not rule.evaluated:
                print(f"  note  {result.objective.name}: {rule.summary()}", file=sys.stderr)
        for name, reason in result.not_covered:
            print(
                f"  note  {result.objective.name}: {name} not covered — {reason}", file=sys.stderr
            )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n")


def _human(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return f"{seconds / size:g}{unit}"
    return f"{seconds}s"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
