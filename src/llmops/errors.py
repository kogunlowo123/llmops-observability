"""The exception hierarchy, and the exit codes the command line maps it to.

Every error carries a ``remedy``: the message says what is wrong, the remedy
says what to do about it. A tool that reports "invalid objective" and stops has
told the user only that they must now read the source.

Exit codes are part of the interface. A CI job distinguishes "an objective was
missed" (2) from "the tool could not run" (3), because those call for different
responses — one is a service that got worse, the other is a broken pipeline —
and a single non-zero code makes them indistinguishable.
"""

from __future__ import annotations

#: Every objective held.
EXIT_OK = 0
#: Reserved for the command line's own usage errors, which argparse emits.
EXIT_USAGE = 1
#: An objective was missed: an error budget burned, a latency target breached, a
#: spend budget exceeded. The tool worked correctly; the service did not.
EXIT_BUDGET_BURNED = 2
#: The tool could not do its job: an unreadable window, an unpriced model, a
#: malformed objective. Nothing was measured, so nothing was proven either way.
EXIT_ERROR = 3


class LlmopsError(Exception):
    """Base class. Carries a remedy alongside the message."""

    def __init__(self, message: str, *, remedy: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.remedy = remedy

    def render(self) -> str:
        """Format for a terminal: the problem, then what to do about it."""
        if self.remedy:
            return f"{self.message}\n  {self.remedy}"
        return self.message


class ConfigError(LlmopsError):
    """An environment variable names a setting this tool does not have."""


class WindowError(LlmopsError):
    """A telemetry window could not be read, parsed or validated."""


class SpanError(LlmopsError):
    """A span is malformed, or an attribute violates a rule."""


class PriceBookError(LlmopsError):
    """A price book could not be loaded, or does not cover what was measured."""


class UnpricedModelError(PriceBookError):
    """A model appeared in the window and the price book has no entry for it.

    Deliberately fatal rather than costed at zero. A spend report that silently
    omits a model is worse than no spend report: it is a number a person will
    put in a slide, and the one model missing from it is by construction the
    newest and most expensive one.
    """


class ObjectiveError(LlmopsError):
    """An objective definition is unusable."""


class CardinalityError(LlmopsError):
    """A label would make a metric series unbounded.

    Raised at *registration*, not at emit time. A guard that fires when the
    first bad label value arrives has already let a caller build a metric whose
    whole purpose is forbidden, and by then the series exists.
    """


class ExporterError(LlmopsError):
    """An exporter could not deliver, or was constructed in an offline run."""


class NetworkNotAllowedError(ExporterError):
    """An exporter that opens a socket was built in an offline run.

    Raised at construction. The default for this tool is that nothing leaves the
    machine: a telemetry pipeline is exactly the component that should not
    surprise anyone by sending somewhere.
    """
