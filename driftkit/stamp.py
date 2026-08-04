#!/usr/bin/env python3
"""stamp.py: a run stamp under every report.

Written after a case that cost trust. A pull request to Boost.GIL quoted "42
mismatches in GIL, 25 in Histogram". Both numbers came from a broken version
of the scanner; the real ones were 11 and 3. Marshall Clow, the author of the
library, could have gone and checked them.

The rule: numbers about someone else's code are quoted only from a fixed tool.
The stamp makes that checkable. Every report carries the tool name, a
fingerprint of its sources and the date of the run, so a number seen in a
thread can be traced back to the run that produced it.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Sequence


def fingerprint(*paths: str) -> str:
    """Short fingerprint of the tool sources. Changes on any edit."""
    h = hashlib.sha1()
    for p in sorted(paths):
        try:
            with open(p, "rb") as fh:
                h.update(fh.read())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:8]


def line(tool_file: str, deps: Sequence[str] = ()) -> str:
    here = os.path.dirname(os.path.abspath(tool_file))
    files = [tool_file] + [os.path.join(here, d) for d in deps]
    return (
        f"  run:                    {os.path.basename(tool_file)} "
        f"fingerprint {fingerprint(*files)}, {time.strftime('%Y-%m-%d %H:%M')}"
    )
