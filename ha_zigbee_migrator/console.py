"""Small terminal presentation helpers with safe plain-text fallbacks."""

from __future__ import annotations

import os
import sys
from typing import TextIO


RESET = "\033[0m"
BOLD = "1"
RED = "31"
GREEN = "32"
YELLOW = "33"
CYAN = "36"
DIM = "2"


def supports_color(stream: TextIO | None = None) -> bool:
    """Return whether ANSI color should be emitted for this stream."""
    stream = stream or sys.stdout
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("CLICOLOR_FORCE") not in {None, "", "0"}:
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def styled(
    text: str,
    *codes: str,
    stream: TextIO | None = None,
) -> str:
    """Style text only when its destination is an interactive color terminal."""
    if not supports_color(stream):
        return text
    return f"\033[{';'.join(codes)}m{text}{RESET}"


def heading(text: str, stream: TextIO | None = None) -> str:
    return styled(text, BOLD, CYAN, stream=stream)


def success(text: str, stream: TextIO | None = None) -> str:
    return styled(text, GREEN, stream=stream)


def warning(text: str, stream: TextIO | None = None) -> str:
    return styled(text, YELLOW, stream=stream)


def failure(text: str, stream: TextIO | None = None) -> str:
    return styled(text, BOLD, RED, stream=stream or sys.stderr)


def status_value(status: str) -> str:
    """Color a migration status by whether it is complete, active, or blocked."""
    if status == "verified":
        return success(status)
    if status == "retry":
        return styled(status, RED)
    if status == "skip":
        return warning(status)
    if status in {"mapped", "paired", "renamed", "interviewed", "pending"}:
        return styled(status, CYAN)
    return status


def print_error(message: str) -> None:
    """Render an actionable, visually distinct error without a traceback."""
    print(file=sys.stderr)
    print(failure("ERROR", stream=sys.stderr), file=sys.stderr)
    for line in str(message).splitlines() or [str(message)]:
        print(f"  {line}", file=sys.stderr)
    print(
        warning(
            "  Workflow stopped safely; completed checkpoints remain resumable.",
            stream=sys.stderr,
        ),
        file=sys.stderr,
    )
