#!/usr/bin/env python3
"""common.py: the contract shared by every tool in this kit.

Written once there were eight tools and they had quietly drifted apart:

  - `liftdrift` called its confidence flag `confident` in JSON while everyone
    else called it `hard`. The sweep runner read `hard` defaulting to True and
    **counted soft findings as hard**. A real bug, found only by comparing the
    tools against each other;
  - "do not touch the network" was `--offline` in one place and `--no-proof`
    in two others;
  - the list of skipped directories was declared three times, differently.

Hence the rule: **the kit is one thing, not eleven separate things.** The
contract is written below and a conformance test checks that every tool obeys
it. A contract nobody checks drifts apart within a week.

TOOL CONTRACT

  1. Flags: `--json FILE` and `-v/--verbose` exist everywhere. Where the network
     is involved, `--offline` turns it off.
  2. `--json` writes a list of objects, each carrying a boolean `hard`.
     A hard finding is one the tool stands behind. A soft one is where a
     human decides.
  3. The report ends with a `=== Coverage ===` block whose last two lines are
     `findings: N hard, M soft` and the run stamp.
  4. Exit code is 1 if and only if there is at least one hard finding, so the
     tool can be used in a shell `if`.
"""

from __future__ import annotations

import os
import re
from typing import Iterator, List, Sequence, Tuple

# Directories no tool in this kit reads. The single place this list is
# declared: three separate copies had already drifted apart.
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", "vendor", "node_modules", "testdata", "third_party",
    "dist", "build", "_build", "target", "__pycache__", ".venv", "venv",
})

# `.github` starts with a dot but is needed: CI matrices live there. A blanket
# hidden-directory mask once dropped it, and the survey quietly declared a
# project unusable while it had nineteen workflows.
KEEP_HIDDEN = frozenset({".github"})


def walk(root: str) -> Iterator[Tuple[str, List[str], List[str]]]:
    """Walk a tree using the shared skip list."""
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [
            d for d in dirs
            if d not in SKIP_DIRS and (d in KEEP_HIDDEN or not d.startswith("."))
        ]
        yield dirpath, dirs, names


def read_text(path: str, limit: int = 4_000_000) -> str:
    """Read a file, tolerating broken encodings and huge files."""
    try:
        if os.path.getsize(path) > limit:
            return ""
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def line_of(text: str, needle: str, start: int = 0) -> int:
    """Line number of the first occurrence. 1-based."""
    for i, ln in enumerate(text.splitlines()[start:], start + 1):
        if needle in ln:
            return i
    return 1


def add_common_args(ap, network: bool = False) -> None:
    """Flags every tool is required to have."""
    ap.add_argument("--json", help="write findings to JSON")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="list what was dismissed and what went unmatched")
    if network:
        ap.add_argument("--offline", "--no-proof", dest="offline", action="store_true",
                        help="stay offline; findings will remain soft")


def findings_line(hard: int, soft: int) -> str:
    return f"  findings:               {hard} hard, {soft} soft"
