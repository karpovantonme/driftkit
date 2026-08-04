#!/usr/bin/env python3
"""assertdrift.py: a test that does not check what it claims to check.

The test is written, it runs in CI, it is green, and it guards something other
than what its name says, or nothing at all.

THE MAIN THING TO KNOW BEFORE RUNNING IT. Of the four known cases of this
species, **three are already caught by a linter most projects already run**:

  toqito  - `pytest.raises(Exception)` is too broad        → ruff B017
  pyvo    - two lines with no assert, expression discarded → ruff B015
  MNE     - `==` instead of `=`, a comparison with no effect → ruff B015

Those need no separate tool, only a flag in a run that already happens:
`ruff check --isolated --select B015,B017`.

The fourth case, traefik, is caught by **no** linter, and this tool is exactly
about it. In helm-unittest suites a child key of an assertion accidentally ends
up as its sibling:

    asserts:
      - failedTemplate:
        errorMessage: "ERROR: ..."     # <- same indentation as failedTemplate

YAML reads that as two sibling keys. `failedTemplate` without a nested
`errorMessage` only checks that rendering failed at all; the error text is never
compared. The test is green and useless at the same time.

WHAT LEGITIMATELY SITS AS A SIBLING rather than inside, and is not a finding:
`template`, `documentIndex`, `documentSelector`, `not`, `chartSelector`.
These are assertion modifiers, siblings by design. Without this list the tool
reports 85 "findings" on a single traefik chart.

A MANDATORY STEP BEFORE SUBMITTING: show the mutation. Replace the expected
text and confirm the test **fails**. If it does not, the test really is not
comparing it. The tool prints a ready mutation command for every finding:
without one, the finding reads as nitpicking about indentation.

Run:
  python3 assertdrift.py --dir ~/Projects/oss/traefik/traefik-helm-chart
  python3 assertdrift.py --dir ... -v

Tests: test_assertdrift.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Set

try:
    import yaml
except ImportError:
    yaml = None

# Assertion -> keys that are required to live INSIDE it
ASSERT_CHILDREN: Dict[str, Set[str]] = {
    "failedTemplate": {"errorMessage", "errorPattern"},
    "equal": {"path", "value", "decodeBase64"},
    "notEqual": {"path", "value"},
    "matchRegex": {"path", "pattern"},
    "notMatchRegex": {"path", "pattern"},
    "contains": {"path", "content", "count", "any"},
    "notContains": {"path", "content", "any"},
    "isNull": {"path"},
    "isNotNull": {"path"},
    "isEmpty": {"path"},
    "isNotEmpty": {"path"},
    "hasDocuments": {"count"},
    "isKind": {"of"},
    "isAPIVersion": {"of"},
    "isSubset": {"path", "content"},
    "matchSnapshot": {"path"},
    "exists": {"path"},
    "greaterOrEqual": {"path", "value"},
    "lessOrEqual": {"path", "value"},
}

# Keys that sit as siblings of an assertion BY DESIGN. Without this list the
# tool produces 85 false findings on a single traefik chart.
LEGIT_SIBLINGS = {
    "template", "templates", "documentIndex", "documentSelector",
    "not", "chartSelector", "skip", "description",
}


@dataclass
class Finding:
    file: str
    line: int
    test_name: str
    assertion: str
    stray: List[str]
    probe: str
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    files: int = 0
    tests: int = 0
    assertions: int = 0
    legit_siblings: int = 0
    unparsed: List[str] = dc_field(default_factory=list)
    findings: List[Finding] = dc_field(default_factory=list)


def _line_of(text: str, needle: str, start: int = 0) -> int:
    for i, ln in enumerate(text.splitlines()[start:], start + 1):
        if needle in ln:
            return i
    return 1


def scan_file(path: str, root: str, report: Report) -> None:
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    try:
        docs = list(yaml.safe_load_all(text))
    except Exception as exc:  # noqa: BLE001
        report.unparsed.append(f"{os.path.relpath(path, root)} - {str(exc)[:60]}")
        return
    report.files += 1
    rel = os.path.relpath(path, root)

    for doc in docs or []:
        if not isinstance(doc, dict):
            continue
        for test in doc.get("tests") or []:
            if not isinstance(test, dict):
                continue
            report.tests += 1
            name = str(test.get("it", "unnamed"))[:80]
            for a in test.get("asserts") or []:
                if not isinstance(a, dict):
                    continue
                report.assertions += 1
                keys = set(a)
                used = keys & set(ASSERT_CHILDREN)
                if len(used) != 1:
                    continue
                assertion = next(iter(used))
                children = ASSERT_CHILDREN[assertion]
                stray = sorted((keys - {assertion} - LEGIT_SIBLINGS) & children)
                report.legit_siblings += len(keys & LEGIT_SIBLINGS)
                if not stray:
                    continue
                # If the assertion already has nested content, the sibling key
                # may be something else entirely. It is a finding when the
                # assertion is empty or scalar, i.e. there is nothing to compare.
                inner = a.get(assertion)
                if isinstance(inner, dict) and inner:
                    continue
                report.findings.append(
                    Finding(
                        file=rel,
                        line=_line_of(text, f"{assertion}:"),
                        test_name=name,
                        assertion=assertion,
                        stray=stray,
                        probe=(
                            f"replace the value of {stray[0]} with a knowingly wrong one "
                            f"and run helm unittest: the test must fail"
                        ),
                        detail=[
                            f"  `{assertion}:` is empty while `{', '.join(stray)}` sits beside it",
                            f"  test: {name}",
                        ],
                    )
                )


def analyse(root: str, report: Report) -> None:
    if yaml is None:
        sys.exit("pyyaml required: pip3 install pyyaml")
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "vendor"]
        for n in sorted(names):
            if n.endswith(("_test.yaml", "_test.yml")):
                scan_file(os.path.join(dirpath, n), root, report)


def print_report(report: Report, verbose: bool = False) -> None:
    if report.findings:
        print(f"\n=== Assertion does not compare what it claims ({len(report.findings)}) ===")
        for f in report.findings:
            print(f"\n[{f.assertion}] {f.file}:{f.line}")
            for d in f.detail:
                print(d)
            print(f"  mutation: {f.probe}")

    print("\n=== Coverage ===")
    print(f"  test files:             {report.files}")
    print(f"  tests:                  {report.tests}")
    print(f"  assertions:             {report.assertions}")
    print(f"  legitimate siblings:    {report.legit_siblings} (template, documentIndex, not)")
    print(f"  not parsed:             {len(report.unparsed)}")
    print(common.findings_line(len(report.findings), 0))
    print(stamp.line(__file__, []))

    if verbose and report.unparsed:
        print(f"\n--- Not parsed ({len(report.unparsed)}) ---")
        for i in report.unparsed[:30]:
            print(f"  {i}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Tests that do not check what they claim")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    report = Report()
    analyse(args.dir, report)
    print_report(report, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "hard": True, "file": f.file, "line": f.line, "test": f.test_name,
                        "assertion": f.assertion, "stray": f.stray, "probe": f.probe,
                    }
                    for f in report.findings
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if report.findings else 0


if __name__ == "__main__":
    sys.exit(main())
