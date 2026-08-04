#!/usr/bin/env python3
"""probe.py: prove that a test guards what it claims to guard.

The third stage of the pipeline. The refuter removes false findings; the prober
turns the survivors into **proven** ones.

WHY. A finding of the "test does not check what it claims" species is not worth
submitting without a mutation: it reads as nitpicking about indentation. The
rule belongs to the species itself, show that the test fails when it should.
Until now that was done by hand.

HOW IT WORKS. Three steps, and the third matters more than the first two:

  1. replace the expected value with a knowingly wrong one;
  2. run the tests with the same command the project CI uses;
  3. **put the file back exactly as it was, whatever happens.**

If the test **fails** after the mutation, it does compare the expected value and
the finding is false. If it **passes**, it does not compare it, and that is a
proof a maintainer reproduces in a minute.

The traefik case was proven exactly this way: with the indentation fixed the
mutation breaks the test, without the fix it does not, because YAML reads
`errorMessage` as a sibling key and the message is never compared.

ABOUT SOMEONE ELSE'S WORKING TREE. The tool edits a file inside the project's
working tree, so it owes a byte-for-byte restore. The restore lives in `finally`
and the content is compared against the original before exit. If the restore
fails, the tool says so in its own line instead of finishing quietly.

WHAT THE PROBER DOES NOT DO:
  - it does not judge the substance of a finding. It answers exactly one
    question: does the check compare what is written in it;
  - it runs nothing it was not told to run. The test command comes from a flag,
    the tool never invents one;
  - it leaves the tree alone when git reports it dirty, otherwise someone else's
    edits get mixed into the mutation.

KNOWN BLIND SPOTS:
  - **a green baseline is mandatory.** If the tests fail without the mutation
    there is no answer at all: the tool says so and leaves;
  - **a test that fails for the wrong reason.** A mutation can break the run
    through format parsing rather than comparison. That is visible only by eye
    in the tail of the output, which is why the tail is printed;
  - **flaky suites.** A run that fails every other time gives a random answer
    and the tool cannot tell the difference;
  - **anything that is not `key: value` in text.** The mutation is textual, so
    binary and generated expectations are out of reach.

Run:
  python3 probe.py --file traefik/tests/requirements-config_test.yaml \\
      --key errorMessage --cmd "helm unittest traefik" --cwd ~/Projects/oss/traefik/traefik-helm-chart
  python3 probe.py --file x_test.yaml --key value --cmd "..." --dry-run

Tests: test_probe.py next to this file.
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
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

MUTATION = "KNOWINGLY-WRONG-VALUE-probe"


@dataclass
class Result:
    file: str
    key: str
    line: int = 0
    before: str = ""
    after: str = ""
    baseline_failed: Optional[bool] = None
    mutated_failed: Optional[bool] = None
    restored: bool = False
    notes: List[str] = dc_field(default_factory=list)

    @property
    def proven(self) -> bool:
        """Proven that the check does NOT compare what it claims."""
        return self.baseline_failed is False and self.mutated_failed is False

    @property
    def verdict(self) -> str:
        if self.baseline_failed is None:
            return "not checked"
        if self.baseline_failed:
            return "tests fail without the mutation, get a green run first"
        if self.mutated_failed:
            return "the check does compare it, so the finding is false"
        return "the check does NOT compare it, proven by mutation"


# --------------------------------------------------------------------------


def find_key(text: str, key: str, near: int = 0) -> Optional[Tuple[int, str, str]]:
    """The line holding `key: value`. Returns (number, indent+key, value)."""
    rx = re.compile(r"^(\s*-?\s*" + re.escape(key) + r"\s*:\s*)(.+?)\s*$")
    best = None
    for i, ln in enumerate(text.splitlines(), 1):
        m = rx.match(ln)
        if not m:
            continue
        if near and best is not None and abs(best[0] - near) <= abs(i - near):
            continue
        best = (i, m.group(1), m.group(2))
        if not near:
            break
    return best


def mutate(text: str, line: int, prefix: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    tail = "\n" if lines[line - 1].endswith("\n") else ""
    quote = '"' if value.startswith(('"', "'")) else ""
    lines[line - 1] = f"{prefix}{quote}{MUTATION}{quote}{tail}"
    return "".join(lines)


def tree_is_dirty(cwd: str) -> bool:
    r = subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def run_tests(cmd: str, cwd: str, timeout: int = 600) -> Tuple[bool, str]:
    """Returns (did it fail, tail of the output)."""
    try:
        p = subprocess.run(
            shlex.split(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return True, "the run did not finish within the time limit"
    except OSError as e:
        return True, f"could not start the command: {e}"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode != 0, out[-1500:]


def probe(path: str, key: str, cmd: str, cwd: str, near: int = 0, dry: bool = False) -> Result:
    res = Result(file=path, key=key)
    full = path if os.path.isabs(path) else os.path.join(cwd, path)
    original = common.read_text(full)
    if not original:
        res.notes.append(f"file not read: {full}")
        return res

    found = find_key(original, key, near)
    if not found:
        res.notes.append(f"key `{key}` not found in the file, nothing to mutate")
        return res
    res.line, prefix, value = found
    res.before = value

    if tree_is_dirty(cwd):
        res.notes.append(
            "the working tree is dirty per git, so the mutation would mix with "
            "someone else's edits and the probe is skipped"
        )
        return res

    mutated = mutate(original, res.line, prefix, value)
    res.after = MUTATION
    if dry:
        res.notes.append("dry run: the file was untouched and no tests ran")
        return res

    try:
        failed, out = run_tests(cmd, cwd)
        res.baseline_failed = failed
        if failed:
            res.notes.append("before the mutation: " + out.strip().splitlines()[-1][:120] if out.strip() else "failed before the mutation")
            return res
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(mutated)
        res.mutated_failed, out = run_tests(cmd, cwd)
        if out.strip():
            res.notes.append("after the mutation: " + out.strip().splitlines()[-1][:120])
    finally:
        # The restore is mandatory on any outcome, exceptions included.
        try:
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(original)
            res.restored = common.read_text(full) == original
        except OSError as e:  # noqa: BLE001
            res.restored = False
            res.notes.append(f"COULD NOT RESTORE THE FILE: {e}")
    return res


# --------------------------------------------------------------------------


def print_report(res: Result, verbose: bool = False) -> None:
    print(f"\n=== Mutation probe: {res.file} ===")
    print(f"  key:                    {res.key}" + (f" (line {res.line})" if res.line else ""))
    if res.before:
        print(f"  was:                    {res.before[:70]}")
        print(f"  replaced with:          {res.after}")
    print(f"  verdict:                {res.verdict}")
    if res.baseline_failed is not None:
        print(f"  file restored:          {'yes' if res.restored else 'NO, check by hand'}")
    for n in res.notes:
        print(f"  {n}")
    hard = 1 if res.proven else 0
    print("\n=== Coverage ===")
    print(f"  test runs:              {0 if res.baseline_failed is None else (1 if res.mutated_failed is None else 2)}")
    print(common.findings_line(hard, 0))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Prove that a check does not compare what it claims")
    ap.add_argument("--file", required=True, help="the test file")
    ap.add_argument("--key", required=True, help="the key whose value gets replaced")
    ap.add_argument("--cmd", required=True, help="the project command that runs the tests")
    ap.add_argument("--cwd", default=".", help="directory to run from")
    ap.add_argument("--near", type=int, default=0, help="line number the key should be closest to")
    ap.add_argument("--dry-run", action="store_true", help="change nothing and run nothing")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    res = probe(args.file, args.key, args.cmd, args.cwd, args.near, args.dry_run)
    print_report(res, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "hard": res.proven, "file": res.file, "key": res.key,
                        "line": res.line, "verdict": res.verdict,
                        "baseline_failed": res.baseline_failed,
                        "mutated_failed": res.mutated_failed,
                        "restored": res.restored, "notes": res.notes,
                    }
                ] if res.line else [],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if res.proven else 0


if __name__ == "__main__":
    sys.exit(main())
