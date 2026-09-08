"""The task table. Run through ``python tasks.py <name>``.

Stdlib only, because a task runner that needs its own dependency installed
before it can install dependencies is a bootstrap problem nobody asked for.

``uv run`` picks the group each task actually needs rather than installing
everything: the documentation build gets a renderer and not pytest, and the
example commands get the project alone. A Pages job that installs mypy is three
minutes of a runner spent on a tool it will not run.
"""

from __future__ import annotations

from collections.abc import Sequence

IMAGE = "llmops-observability"

WINDOWS = "examples/windows"
OBJECTIVES = "examples/objectives.yaml"
PRICES = "examples/prices.yaml"

#: The instants the shipped scenarios are demonstrated at. Each incident is over
#: by the end of its window — which is correct, and the whole point of the short
#: window — so a gate run at the window's end sees a platform that has already
#: recovered. These are the moments each incident was live.
AT = {
    "outage": "2026-09-01T22:40:00+00:00",
    "slowdown": "2026-09-02T12:00:00+00:00",
    "cost-spike": "2026-09-03T12:00:00+00:00",
}

SCENARIOS = ("steady", "outage", "slowdown", "cost-spike")


def _run(*args: str) -> list[str]:
    return ["uv", "run", *args]


def _gate(scenario: str, *extra: str) -> list[str]:
    command = _run(
        "python",
        "-m",
        "llmops",
        "gate",
        "--window",
        f"{WINDOWS}/{scenario}.jsonl.gz",
        "--objectives",
        OBJECTIVES,
        "--pricebook",
        PRICES,
    )
    if scenario in AT:
        command += ["--at", AT[scenario]]
    return command + list(extra)


TASKS: dict[str, tuple[str, list[Sequence[str]]]] = {
    "setup": (
        "Install the project and its development tooling.",
        [["uv", "sync", "--locked", "--group", "dev", "--group", "docs"]],
    ),
    "fmt": ("Format.", [_run("ruff", "format", ".")]),
    "lint": (
        "Lint and check formatting.",
        [_run("ruff", "check", "."), _run("ruff", "format", "--check", ".")],
    ),
    "typecheck": ("Type check under mypy --strict.", [_run("mypy")]),
    "test": (
        "Run the whole test suite with the coverage gate.",
        [_run("pytest", "--cov", "--cov-report=term-missing", "--cov-fail-under=90")],
    ),
    "test-unit": ("Unit tests only.", [_run("pytest", "-m", "unit")]),
    "test-integration": ("Integration tests only.", [_run("pytest", "-m", "integration")]),
    "test-security": ("Security tests only.", [_run("pytest", "-m", "security")]),
    "test-e2e": (
        "End-to-end tests only: the CLI as a real process.",
        [_run("pytest", "-m", "e2e")],
    ),
    "test-meta": (
        "The gate's own negative controls: break one thing, assert it goes red.",
        [_run("pytest", "-m", "meta")],
    ),
    "gate": (
        "Gate the healthy window. The control: it has to be green.",
        [_gate("steady")],
    ),
    "check-incidents": (
        "Assert the gate goes red on each incident, and red for the right reason.",
        [_run("python", "scripts/check-incidents.py")],
    ),
    "cost": (
        "What the healthy window cost, by model.",
        [
            _run(
                "python",
                "-m",
                "llmops",
                "cost",
                "--window",
                f"{WINDOWS}/steady.jsonl.gz",
                "--pricebook",
                PRICES,
                "--by",
                "model",
            )
        ],
    ),
    "doctor": (
        "Report what this installation would do, and self-check end to end.",
        [_run("python", "-m", "llmops", "doctor", "--window", f"{WINDOWS}/steady.jsonl.gz")],
    ),
    "corpus": (
        "Regenerate every example window from its scenario.",
        [
            _run(
                "python",
                "-m",
                "llmops",
                "synth",
                "--scenario",
                name,
                "--out",
                f"{WINDOWS}/{name}.jsonl.gz",
            )
            for name in SCENARIOS
        ],
    ),
    "corpus-check": (
        "Do the committed windows still match their scenarios? The CI check.",
        [
            _run(
                "python",
                "-m",
                "llmops",
                "check",
                "--scenario",
                name,
                "--window",
                f"{WINDOWS}/{name}.jsonl.gz",
            )
            for name in SCENARIOS
        ],
    ),
    "examples": (
        "Run every example. They are documentation that executes.",
        [
            _run("python", "examples/quickstart.py"),
            _run("python", "examples/burnrate_demo.py"),
            _run("python", "examples/cardinality_demo.py"),
        ],
    ),
    "site": (
        "Build the documentation site into _site.",
        [_run("--only-group", "docs", "python", "scripts/build_site.py", "--output", "_site")],
    ),
    "security": (
        "Local security scans.",
        [
            _run("bandit", "-c", "pyproject.toml", "-r", "src", "-f", "screen"),
            [
                "uv",
                "export",
                "--locked",
                "--no-emit-project",
                "--no-hashes",
                "--output-file",
                "requirements.audit.txt",
            ],
            [
                "uv",
                "tool",
                "run",
                "pip-audit",
                "--strict",
                "--no-deps",
                "--requirement",
                "requirements.audit.txt",
            ],
        ],
    ),
    "docker-build": (
        "Build the container image.",
        [["docker", "build", "-t", f"{IMAGE}:local", "."]],
    ),
    "smoke": (
        "Build the image and run the smoke test against it.",
        [
            ["docker", "build", "-t", f"{IMAGE}:local", "."],
            ["bash", "scripts/smoke-test.sh", f"{IMAGE}:local"],
        ],
    ),
}

#: The order CI runs things in, cheapest gate first. Formatting and typing fail
#: in seconds; the container build takes minutes. A developer who broke an import
#: should learn that before the suite has finished collecting.
ALL = (
    "lint",
    "typecheck",
    "test",
    "corpus-check",
    "gate",
    "check-incidents",
    "examples",
    "site",
)

# Expanded here rather than special-cased in a runner: tasks.py runs whatever
# command list it finds, and `all` used to carry an empty one, which made
# `python tasks.py all` print nothing and exit 0. Nothing about that looked
# wrong on a terminal, which is what made it worth catching.
TASKS["all"] = (
    "Everything CI runs, in the order CI runs it.",
    [step for name in ALL for step in TASKS[name][1]],
)
