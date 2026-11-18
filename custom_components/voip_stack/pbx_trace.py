"""Human-readable call decisions in Home Assistant's standard integration log."""

from __future__ import annotations

import logging
import re

# Log parties, destinations and policy decisions. URI parameters and credentials
# are transport details; preserve the dialled user even when it contains "+".
_URI = re.compile(r"\bsips?:([^\s<>]+)", re.IGNORECASE)
_BARE_CREDENTIALS = re.compile(r"(?<![\w:])([^\s:@<>]+):[^\s@<>]+@")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f]+")


def _label(value: object) -> str:
    text = str(value) if value is not None else ""
    text = _URI.sub(lambda match: match[1].split("@", 1)[0].split(":", 1)[0]
                    .split(";", 1)[0].split("?", 1)[0], text)
    text = _BARE_CREDENTIALS.sub(r"\1@", text)
    return " ".join(_CONTROLS.sub(" ", text).split())[:256] or "unknown"


def trace_call(logger: logging.Logger, call_id: str, message: str, *args: object,
               level: int = logging.DEBUG) -> None:
    """Log a single decision with the call reference at the end for filtering."""
    if logger.isEnabledFor(level):
        logger.log(level, "PBX | " + message + " [call_id=%s]",
                   *(_label(value) for value in args), _label(call_id))
