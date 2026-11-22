"""Redact before truncating so cutting a value cannot expose its prefix."""

import re

_BEARER = re.compile(r"\bBearer\s+(?:\[redacted\]|[^\s\"'<>,;&}\])])+", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"(?P<prefix>(?<![a-z0-9_-])(?P<key_quote>[\"']?)[a-z0-9_-]*"
    r"(?:password|passwd|secret|token|api_key|apikey|api-key|authorization|private_key|credential)"
    r"[a-z0-9_-]*(?P=key_quote)[ \t]*[:=][ \t]*)"
    r"(?:(?P<quote>[\"'])(?:\\[^\r\n]?|(?!(?P=quote))[^\\\r\n])*(?P=quote)?"
    r"|(?:\[redacted\]|[^\s,;&}\])\"'])(?:\[redacted\]|[^\s,;&}\])])*)",
    re.IGNORECASE,
)
_AUTHORIZATION = re.compile(
    r"(?<![a-z0-9_-])(Authorization[ \t]*[:=][ \t]*)"
    r"(?:\[redacted\]|[^\s\"',;&}\])])(?:\[redacted\]|[^\r\n\"',;&}\])])*",
    re.IGNORECASE,
)


def redact(text: str, *, secret: str = "") -> str:
    if secret:
        text = text.replace(secret, "[redacted]")
    text = _AUTHORIZATION.sub(r"\1[redacted]", text)
    text = _BEARER.sub("Bearer [redacted]", text)
    # Treat earlier replacement markers as whole values, including their closing ].
    return _ASSIGNMENT.sub(
        lambda match: match["prefix"] + (match["quote"] or "") + "[redacted]" + (match["quote"] or ""),
        text,
    )
