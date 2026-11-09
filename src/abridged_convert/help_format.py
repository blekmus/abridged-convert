"""Colour for argparse help, usage, and error output (Python 3.12 argparse has none built in).

The help text is formatted plain first and coloured afterwards, so argparse's width
calculations never see escape codes.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import IO

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

HEADING = re.compile(r"^(?P<heading>[a-z][a-z ]*:)(?P<rest>.*)$")
COMMENT = re.compile(r"^\s+#")
ERROR = re.compile(r"^(?P<prog>.*?: )(?P<label>error:)(?P<rest>.*)$")
TOKENS = re.compile(
    r"(?P<command>(?:(?<=abridged-convert )|(?<=^    ))(?:convert|deploy|rebuild-metadata)\b)"
    r"|(?P<choice>\{[A-Za-z,-]+\})"
    r"|(?P<prog>\babridged-convert\b)"
    r"|(?P<flag>(?<![\w-])--?[a-z][\w-]*)"
    r"|(?P<meta>\b[A-Z][A-Z_]{2,}\b)"
    r"|(?P<note>\(not [^)]*\))"
)
TOKEN_STYLES = {"command": BOLD + CYAN, "choice": MAGENTA, "prog": BOLD, "flag": CYAN, "meta": GREEN, "note": DIM}


def color_enabled(stream: IO[str] | None) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return stream is not None and hasattr(stream, "isatty") and stream.isatty()


def _color_tokens(text: str) -> str:
    return TOKENS.sub(lambda match: f"{TOKEN_STYLES[match.lastgroup]}{match.group()}{RESET}", text)


def colorize(text: str) -> str:
    lines = []
    for line in text.split("\n"):
        if COMMENT.match(line):
            lines.append(f"{DIM}{line}{RESET}")
        elif error := ERROR.match(line):
            lines.append(f"{error['prog']}{BOLD}{RED}{error['label']}{RESET}{error['rest']}")
        elif heading := HEADING.match(line):
            lines.append(f"{BOLD}{YELLOW}{heading['heading']}{RESET}{_color_tokens(heading['rest'])}")
        else:
            lines.append(_color_tokens(line))
    return "\n".join(lines)


class ColorArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that colours help, usage, and errors when writing to a terminal.

    Subparsers inherit this class, since ``add_subparsers`` defaults to the parent's type.
    """

    def _print_message(self, message: str, file: IO[str] | None = None) -> None:
        if message and color_enabled(file):
            message = colorize(message)
        super()._print_message(message, file)
