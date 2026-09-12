#!/usr/bin/env python3
"""racedrift.py: what a project promises about concurrency against what it does.

The first check in this kit that judges **behaviour** rather than text. Every
other tool compares two written statements; this one runs the project's own test
suite under the race detector and reads the answer.

WHY IT LIVES ON A RUNNER AND NOT ON A LAPTOP. Running a suite means executing
somebody else's code: their build scripts, their fixtures, their network calls.
The kit refuses to do that by default anywhere, and `common.PLACE` marks this
check `build`, which means a disposable machine. On a laptop it runs only when
somebody says so out loud with `--here`.

TWO SHAPES OF FINDING, and the first is the interesting one:

  **promised and broken.** The project's CI advertises `-race` and the suite
      does not survive it. Someone changed a workflow, or the job is scoped to
      one branch, and nobody noticed. This is a real defect with a reproduction
      the maintainer runs in one command.

  **not promised and broken.** No `-race` anywhere in CI, and the suite fails
      under it. Worth reporting as a race, worth reporting as a missing CI job,
      and worth saying which of the two we mean.

WHAT IT DOES NOT DO:
  - it does not fix races and does not name the guilty variable beyond what the
    detector prints;
  - it does not run anything but the project's own tests, with the project's own
    command;
  - it does not touch a tree that git reports as dirty;
  - Go only for now. The C++ and Python halves of this family need a sanitizer
    build and a test runner respectively, and neither is written yet. Saying so
    is cheaper than a report that quietly covered one language.

Run:
  python3 racedrift.py --dir ~/src/some-go-project        # refuses: build check
  python3 racedrift.py --dir ~/src/some-go-project --here # says so out loud
  python3 racedrift.py --dir /work/target --json out.json # how CI calls it

Tests: test_racedrift.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field as dc_field
from typing import List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import buildprobe  # noqa: E402
import common  # noqa: E402
import stamp  # noqa: E402

RACE_HEADER = re.compile(r"^(WARNING: DATA RACE|==\d+==ERROR: .*Sanitizer)", re.M)
RACE_FILE = re.compile(r"^\s+(/[^\s:]+\.go):(\d+)", re.M)
FAIL_LINE = re.compile(r"^(FAIL|--- FAIL:)\s+(\S+)", re.M)


@dataclass
class Finding:
    hard: bool
    kind: str
    file: str
    line: int
    subject: str
    message: str
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    root: str = ""
    system: str = ""
    promised: List[str] = dc_field(default_factory=list)
    ran: bool = False
    seconds: float = 0.0
    races: int = 0
    failures: int = 0
    findings: List[Finding] = dc_field(default_factory=list)
    notes: List[str] = dc_field(default_factory=list)
    tail: str = ""


def promises(root: str) -> List[str]:
    """What the project itself says about the race detector, from its own CI."""
    return [m for m in buildprobe.dynamic_marks(root) if m in ("races", "sanitizers")]


def run_suite(root: str, timeout: int) -> tuple:
    """The project's own command, with a ceiling. Returns (output, seconds, ok)."""
    import time as _t

    cmd = "go test -race ./..."
    started = _t.monotonic()
    try:
        p = subprocess.run(shlex.split(cmd), cwd=root, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"the suite did not finish within {timeout} s", float(timeout), False
    except OSError as e:  # noqa: BLE001
        return f"could not start `{cmd}`: {e}", _t.monotonic() - started, False
    return (p.stdout or "") + (p.stderr or ""), _t.monotonic() - started, p.returncode == 0


def read_output(out: str, root: str, promised: Sequence[str]) -> List[Finding]:
    """Findings from what the detector printed. One race is one finding."""
    findings: List[Finding] = []
    seen = set()
    blocks = RACE_HEADER.split(out)
    for m in RACE_HEADER.finditer(out):
        tail = out[m.end(): m.end() + 2000]
        fm = RACE_FILE.search(tail)
        where, line = (fm.group(1), int(fm.group(2))) if fm else (root, 1)
        try:
            rel = os.path.relpath(where, root)
        except ValueError:
            rel = where
        key = (rel, line)
        if key in seen:
            continue
        seen.add(key)
        findings.append(Finding(
            hard=True,
            kind="race-under-promised-detector" if promised else "race-detector-not-in-ci",
            file=rel, line=line,
            subject=f"data race at {rel}:{line}",
            message=("the CI advertises the race detector and the suite does not survive it"
                     if promised else
                     "the suite does not survive the race detector, which CI never runs"),
            detail=[" " + l for l in tail.splitlines()[:6] if l.strip()],
        ))
    return findings


def analyse(root: str, report: Report, timeout: int, allow_here: bool) -> None:
    report.root = os.path.abspath(root)
    # Probe before refusing: a report that says nothing about the project is
    # less useful than one that says what it would have run and where.
    probe = buildprobe.probe_one(root, run=False)
    report.system = probe.system.name if probe.system else ""
    report.promised = promises(root)
    if not common.runs_here("racedrift", allow_here):
        report.notes.append(
            "this check executes the project's own tests, so its home is a disposable "
            "runner; pass --here to run it on this machine anyway")
        return

    if report.system != "go":
        report.notes.append(
            f"only Go is implemented; this project is {report.system or 'unrecognised'}")
        return
    if not probe.tool_present:
        report.notes.append("`go` is not installed here, nothing to run with")
        return

    out, seconds, ok = run_suite(root, timeout)
    report.ran = True
    report.seconds = seconds
    report.tail = out[-1500:]
    report.races = len(RACE_HEADER.findall(out))
    report.failures = len(FAIL_LINE.findall(out))
    report.findings = read_output(out, root, report.promised)
    if ok and not report.findings:
        report.notes.append("the suite passed under the race detector")
    elif not report.findings and not ok:
        report.notes.append(
            "the suite failed without the detector reporting a race: that is a broken "
            "or flaky suite rather than a finding of ours")


def print_report(report: Report, verbose: bool = False) -> None:
    hard = [f for f in report.findings if f.hard]
    if hard:
        print(f"\n=== Races under the detector ({len(hard)}) ===")
        for f in hard:
            print(f"\n  {f.file}:{f.line}")
            print(f"    {f.message}")
            for d in f.detail[:4]:
                print(f"   {d}")

    print("\n=== Coverage ===")
    print(f"  tree:                   {report.root}")
    print(f"  build system:           {report.system or 'not recognised'}")
    print(f"  CI advertises:          {', '.join(report.promised) or 'nothing about races'}")
    print(f"  suite run:              {'yes' if report.ran else 'no'}"
          + (f", {report.seconds:.0f} s" if report.ran else ""))
    print(f"  races reported:         {report.races}")
    print(f"  test failures:          {report.failures}")
    for n in report.notes:
        print(f"  {n}")
    print(common.findings_line(len(hard), 0))
    print(stamp.line(__file__, ["common.py"]))
    if verbose and report.tail:
        print("\n--- tail of the run ---")
        print(report.tail)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Promises about concurrency against behaviour")
    ap.add_argument("--dir", required=True, help="project directory")
    ap.add_argument("--here", action="store_true",
                    help="run on this machine even though the check belongs on a runner")
    ap.add_argument("--timeout", type=int,
                    default=common.LIMITS["job_timeout_minutes"] * 60,
                    help="ceiling for the suite, seconds")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    report = Report()
    analyse(args.dir, report, args.timeout, args.here)
    print_report(report, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([
                {"hard": f.hard, "kind": f.kind, "file": f.file, "line": f.line,
                 "subject": f.subject, "message": f.message, "detail": f.detail,
                 "tool": "racedrift"}
                for f in report.findings
            ], fh, ensure_ascii=False, indent=1)
    return 1 if any(f.hard for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
