"""Removing credentials from anything on its way out of the process.

This is the third repository in this series to carry a redaction pass, and it is
here for a reason learned the hard way in the second: the mistake is never the
patterns, it is *where the pass runs*. A pass over the inputs leaves anything
derived from them — an excerpt, a summary, a joined string — carrying what the
pass removed.

So the rule, stated once and applied everywhere: **assemble the whole artefact,
then redact it once, immediately before it leaves.** Exporters call
``redact_structure`` on the finished payload, not on the spans that went into
it.

A span has no prompt or completion field at all, which makes this a second line
of defence rather than the only one. What it catches is a credential arriving
through the attribute bag — a base URL with a key in it, an ``Authorization``
value someone attached for debugging, a connection string in a service name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: What a redacted value is replaced with. Fixed-length on purpose: a
#: placeholder that echoed the length of what it replaced would leak it.
PLACEHOLDER = "[redacted]"

#: Attribute names whose *value* is always removed, whatever it looks like.
#: Matched case-insensitively on the whole name after splitting on separators,
#: so `http.request.header.authorization` is caught along with `authorization`.
SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "auth",
        "apikey",
        "api_key",
        "accesstoken",
        "access_token",
        "refreshtoken",
        "refresh_token",
        "bearer",
        "cookie",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "session",
        "signature",
        "token",
        "x-api-key",
    }
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Vendor-shaped API keys. Anchored on the vendor's own prefix, so these are
    # precise: a false positive here removes information from a dashboard.
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("google", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}")),
    ("aws", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    # A bearer token in a header value someone pasted into an attribute.
    ("bearer", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    # A credential in a URL: scheme://user:secret@host. The password is
    # replaced and the rest of the URL is kept, because the host is the useful
    # half and removing it would make the attribute worthless.
    ("url-credentials", re.compile(r"(?P<prefix>://[^\s:@/]+:)[^\s@/]+(?P<suffix>@)")),
    # A JSON Web Token, which carries claims and is frequently a credential.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    # A PEM private key header. The body is long; the header is the tell.
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)


@dataclass(frozen=True, slots=True)
class Redaction:
    """What a pass removed."""

    count: int = 0
    #: Rule names that matched, sorted. Never the values, obviously, and never
    #: a prefix or suffix of one: a "first four characters" hint is a real
    #: attack against a short credential.
    rules: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """Whether nothing was removed."""
        return self.count == 0

    def as_dict(self) -> dict[str, Any]:
        """Serialise for a report."""
        return {"count": self.count, "rules": list(self.rules)}


def redact_text(text: str) -> tuple[str, Redaction]:
    """Replace anything credential-shaped in *text*."""
    if not text:
        return text, Redaction()
    hits: dict[str, int] = {}
    redacted = text
    for name, pattern in _PATTERNS:
        if name == "url-credentials":
            redacted, count = pattern.subn(rf"\g<prefix>{PLACEHOLDER}\g<suffix>", redacted)
        else:
            redacted, count = pattern.subn(PLACEHOLDER, redacted)
        if count:
            hits[name] = hits.get(name, 0) + count
    return redacted, Redaction(count=sum(hits.values()), rules=tuple(sorted(hits)))


def is_sensitive_key(key: str) -> bool:
    """Whether a key's value should be removed regardless of its shape.

    Split on the separators that appear in attribute names, so a nested header
    name is caught by its last segment rather than needing its own entry.
    """
    parts = re.split(r"[._\-/\s]+", key.lower())
    return bool(SENSITIVE_KEYS.intersection({key.lower(), *parts}))


def redact_structure(payload: Any) -> tuple[Any, Redaction]:
    """Redact every string in a nested structure, once, in place of the whole.

    Call this on an **assembled** artefact. See the module docstring: redacting
    the inputs and then deriving a string from them is the bug this function's
    placement is designed to prevent.
    """
    count = 0
    rules: set[str] = set()

    def walk(node: Any, *, key: str = "") -> Any:
        nonlocal count
        if isinstance(node, dict):
            return {name: walk(value, key=str(name)) for name, value in node.items()}
        if isinstance(node, list):
            return [walk(item, key=key) for item in node]
        if isinstance(node, str):
            if key and is_sensitive_key(key) and node:
                count += 1
                rules.add("sensitive-key")
                return PLACEHOLDER
            cleaned, found = redact_text(node)
            if not found.clean:
                count += found.count
                rules.update(found.rules)
            return cleaned
        return node

    return walk(payload), Redaction(count=count, rules=tuple(sorted(rules)))
