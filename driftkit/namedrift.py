#!/usr/bin/env python3
"""namedrift.py: one name spelled two ways.

The fastest species measured so far. AFL++ merged such a fix **within two
hours**, from the lead maintainer personally. The reason is plain: a spelling
mismatch in one name is verified by eye in three seconds and requires no
decision. There is nothing to argue about.

What it looks for: an environment variable, a flag or a config key spelled two
almost identical ways inside one project. The live case: AFL++ documentation
knew `AFL_GCC_ONLY_FSRV` while the registry and the runtime had
`AFL_GCC_ONLY_FRSV`. Follow the docs and you get told to use the typo itself.

HOW IT WORKS. Collect identifier-shaped names across the tree with coordinates,
look for pairs one or two characters apart. The strongest signal is a
**transposition of letters** (FSRV against FRSV): those do not coincide by
chance.

WHAT THE TOOL DOES NOT CALL A FINDING, because those are separate names:
  - one name nested in another: `FOO_ENABLE` and `FOO_DISABLE`, `X` and `X_ALL`;
  - a difference in a digit only: `IPV4` and `IPV6`, `V1` and `V2`;
  - known opposites in the differing segment: MIN/MAX, SRC/DST, IN/OUT, GET/SET,
    READ/WRITE, ADD/DEL and the rest;
  - names shorter than MIN_LEN: short ones have far too many neighbours within
    edit distance two.

WHAT COUNTS AS A SOFT FINDING: a pair occurring **in the same file**. That is
what deliberate compatibility looks like, the old name kept next to the new one.
AFL++ after our own fix #2865 does exactly this: `envs.h` holds both spellings
on purpose.

Run:
  python3 namedrift.py --dir ~/Projects/oss/aflpp
  python3 namedrift.py --dir ... -v        # list everything dismissed, by name

Tests: test_namedrift.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Set, Tuple

MIN_LEN = 8          # shorter names have too many neighbours at distance two
MAX_DIST = 2
RARE_MAX = 10        # at least one spelling has to be rare
MIN_SEGMENT = 5      # the differing segment has to be a word, not an abbreviation

# Where a spelling is visible to a user. A finding is hard only when both
# spellings reach the eye: documentation, a string literal, a getenv call.
# A variable name in code can still be a mismatch, but nobody outside sees it,
# so that goes into a soft line.
DOC_EXT = (".md", ".rst", ".txt", ".adoc")

SKIP_DIRS = common.SKIP_DIRS  # one list for the whole kit
TEXT_EXT = (
    ".c", ".h", ".cc", ".cpp", ".hpp", ".go", ".py", ".rs", ".sh", ".bash", ".java",
    ".js", ".ts", ".rb", ".pl", ".md", ".rst", ".txt", ".yaml", ".yml", ".toml",
    ".cfg", ".ini", ".json", ".tpl", ".gotmpl", ".env", "",
)

# Kinds of names. Each has its own pattern; comparison stays inside one kind.
KINDS = {
    # ENVIRONMENT_VARIABLES and macros: at least three segments, otherwise noise
    "env": re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}\b"),
    # long command line flags
    "flag": re.compile(r"(?<![\w-])--[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b"),
}

# Segments whose difference means separate entities
ANTONYMS = [
    {"min", "max"}, {"src", "dst"}, {"in", "out"}, {"get", "set"},
    {"read", "write"}, {"add", "del"}, {"enable", "disable"}, {"on", "off"},
    {"start", "stop"}, {"first", "last"}, {"pre", "post"}, {"up", "down"},
    {"lo", "hi"}, {"begin", "end"}, {"open", "close"}, {"push", "pull"},
    {"send", "recv"}, {"encode", "decode"}, {"lock", "unlock"},
]


@dataclass
class Finding:
    kind: str
    hard: bool
    shape: str  # transposition | substitution
    a: str
    b: str
    a_count: int
    b_count: int
    a_ref: str
    b_ref: str
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    files: int = 0
    names: int = 0
    pairs_considered: int = 0
    too_short: int = 0
    not_typo_shaped: List[str] = dc_field(default_factory=list)
    nested: List[str] = dc_field(default_factory=list)
    digit_only: List[str] = dc_field(default_factory=list)
    antonym: List[str] = dc_field(default_factory=list)
    both_common: List[str] = dc_field(default_factory=list)
    not_user_facing: List[str] = dc_field(default_factory=list)
    user_facing: Dict[str, Set[str]] = dc_field(default_factory=dict)
    findings: List[Finding] = dc_field(default_factory=list)


# --------------------------------------------------------------------------


def bounded_distance(a: str, b: str, limit: int = MAX_DIST) -> int:
    """Edit distance with an early exit. Anything above limit is not counted."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            best = min(best, cur[j])
        if best > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def _in_quotes(line: str, pos: int) -> bool:
    """Whether the position sits inside a string literal. Quotes on the left are counted."""
    return (line.count('"', 0, pos) % 2 == 1) or (line.count("'", 0, pos) % 2 == 1)


def _segments(name: str) -> List[str]:
    return [s for s in re.split(r"[_\-]+", name.lower().lstrip("-")) if s]


def is_antonym_pair(a: str, b: str) -> bool:
    sa, sb = _segments(a), _segments(b)
    if len(sa) != len(sb):
        # segment counts differ: compare as sets
        diff_a = [x for x in sa if x not in sb]
        diff_b = [x for x in sb if x not in sa]
    else:
        diff_a = [x for x, y in zip(sa, sb) if x != y]
        diff_b = [y for x, y in zip(sa, sb) if x != y]
    for group in ANTONYMS:
        if any(x in group for x in diff_a) and any(y in group for y in diff_b):
            return True
    return False


def differs_only_in_digits(a: str, b: str) -> bool:
    ta = re.sub(r"\d+", "#", a)
    tb = re.sub(r"\d+", "#", b)
    return ta == tb and a != b


def shape_of(a: str, b: str) -> Optional[str]:
    """The shape of the mismatch when it looks like a typo. Otherwise None.

    Exactly two shapes are allowed, and this is the main precision filter:

    **transposition of adjacent letters**, `FSRV` against `FRSV`. Those do not
    coincide by chance, and this is the live AFL++ case.

    **substitution of one character with the segment structure preserved**: the
    same number of underscore-separated pieces, one character apart.

    Insertion and deletion are forbidden on purpose: that is where families of
    related names live. `--disable-docs` against `--disable-bochs`,
    `XXH_HAS_ATTRIBUTE` against `XXH_HAS_C_ATTRIBUTE` are separate entities.
    With insertion allowed the tool produced 162 "findings" on AFL++ alone.
    """
    if len(a) != len(b):
        return None
    diff = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    if len(diff) == 2 and diff[1] == diff[0] + 1:
        i = diff[0]
        if a[i] == b[i + 1] and a[i + 1] == b[i]:
            return "transposition"
    if len(diff) == 1 and a[diff[0]].isalpha() and b[diff[0]].isalpha():
        sa, sb = _segments(a), _segments(b)
        if len(sa) != len(sb):
            return None
        # The differing segment has to be a word. Otherwise families of names get
        # caught: GUM_X86_EBX against GUM_X86_RBX are the 32-bit and 64-bit
        # registers, ARM64_REG_W29 against X29 likewise.
        # On AFL++ those produced 18 "findings" out of 19.
        for x, y in zip(sa, sb):
            if x != y:
                return "substitution" if min(len(x), len(y)) >= MIN_SEGMENT else None
    return None


# --------------------------------------------------------------------------


def collect(root: str, report: Report) -> Dict[str, Dict[str, Dict[str, List[int]]]]:
    """kind -> name -> file -> lines"""
    found: Dict[str, Dict[str, Dict[str, List[int]]]] = {k: defaultdict(lambda: defaultdict(list)) for k in KINDS}
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for n in sorted(names):
            ext = os.path.splitext(n)[1].lower()
            if ext not in TEXT_EXT:
                continue
            path = os.path.join(dirpath, n)
            try:
                if os.path.getsize(path) > 4_000_000:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            report.files += 1
            rel = os.path.relpath(path, root)
            is_doc = ext in DOC_EXT
            for i, line in enumerate(text.splitlines(), 1):
                for kind, rx in KINDS.items():
                    for m in rx.finditer(line):
                        found[kind][m.group(0)][rel].append(i)
                        if is_doc:
                            report.user_facing.setdefault(m.group(0), set()).add("documentation")
                        elif _in_quotes(line, m.start()):
                            report.user_facing.setdefault(m.group(0), set()).add("string")
                        else:
                            report.user_facing.setdefault(m.group(0), set()).add("identifier")
    report.names = sum(len(v) for v in found.values())
    return found


def analyse(root: str, report: Report) -> None:
    tables = collect(root, report)
    for kind, table in tables.items():
        names = [n for n in table if len(n.lstrip("-")) >= MIN_LEN]
        report.too_short += len(table) - len(names)
        # bucket by length: distance of 2 or less requires similar length
        buckets: Dict[int, List[str]] = defaultdict(list)
        for n in names:
            buckets[len(n)].append(n)
        seen: Set[Tuple[str, str]] = set()
        for n in sorted(names):
            for ln in (len(n) - 2, len(n) - 1, len(n), len(n) + 1, len(n) + 2):
                for m in buckets.get(ln, ()):
                    if m == n or (min(n, m), max(n, m)) in seen:
                        continue
                    seen.add((min(n, m), max(n, m)))
                    report.pairs_considered += 1
                    _judge(kind, n, m, table, report)


def _judge(kind: str, a: str, b: str, table, report: Report) -> None:
    if bounded_distance(a, b) > MAX_DIST:
        return
    la, lb = a.lstrip("-"), b.lstrip("-")
    if la in lb or lb in la:
        report.nested.append(f"{a} / {b}: one is nested in the other")
        return
    if differs_only_in_digits(a, b):
        report.digit_only.append(f"{a} / {b}: the difference is a digit")
        return
    if is_antonym_pair(a, b):
        report.antonym.append(f"{a} / {b}: opposites")
        return
    shape = shape_of(a, b)
    if shape is None:
        report.not_typo_shaped.append(f"{a} / {b}: the shape is not typo-like")
        return

    fa, fb = table[a], table[b]
    ca = sum(len(v) for v in fa.values())
    cb = sum(len(v) for v in fb.values())
    if min(ca, cb) > RARE_MAX:
        report.both_common.append(f"{a} ({ca}) / {b} ({cb}): both spellings are common")
        return

    ctx_a = report.user_facing.get(a, set())
    ctx_b = report.user_facing.get(b, set())
    seen_by_user = {"documentation", "string"}
    visible = bool(ctx_a & seen_by_user) and bool(ctx_b & seen_by_user)
    if not visible:
        report.not_user_facing.append(
            f"{a} ({'/'.join(sorted(ctx_a))}) / {b} ({'/'.join(sorted(ctx_b))}): invisible to users"
        )

    shared = set(fa) & set(fb)
    first_a = sorted(fa.items())[0]
    first_b = sorted(fb.items())[0]
    detail = [f"  context: {a} {'/'.join(sorted(ctx_a))}; {b} {'/'.join(sorted(ctx_b))}"]
    if shared:
        detail.append(f"  both spellings occur in {sorted(shared)[0]}, which looks like deliberate compatibility")
    report.findings.append(
        Finding(
            kind=kind,
            hard=not shared and visible,
            shape=shape,
            a=a,
            b=b,
            a_count=ca,
            b_count=cb,
            a_ref=f"{first_a[0]}:{first_a[1][0]}",
            b_ref=f"{first_b[0]}:{first_b[1][0]}",
            detail=detail,
        )
    )


# --------------------------------------------------------------------------


def print_report(report: Report, verbose: bool = False) -> None:
    order = {"transposition": 0, "substitution": 1}
    hard = sorted([f for f in report.findings if f.hard], key=lambda f: order.get(f.shape, 9))
    soft = sorted([f for f in report.findings if not f.hard], key=lambda f: order.get(f.shape, 9))

    def block(title: str, items: List[Finding]) -> None:
        if not items:
            return
        print(f"\n=== {title} ({len(items)}) ===")
        for f in items:
            print(f"\n[{f.shape}] {f.a}  against  {f.b}")
            print(f"  {f.a}: {f.a_count} occurrences, first at {f.a_ref}")
            print(f"  {f.b}: {f.b_count} occurrences, first at {f.b_ref}")
            for d in f.detail:
                print(f"{d}")

    block("One name spelled two ways", hard)
    block("Needs reading by a human", soft)

    print("\n=== Coverage ===")
    print(f"  files read:             {report.files}")
    print(f"  names collected:        {report.names}")
    print(f"  names shorter than {MIN_LEN}: {report.too_short} (too many neighbours)")
    print(f"  pairs considered:       {report.pairs_considered}")
    print(f"  shape not typo-like:    {len(report.not_typo_shaped)} (insertion, deletion, distant)")
    print(f"  one nested in another:  {len(report.nested)}")
    print(f"  digit-only difference:  {len(report.digit_only)}")
    print(f"  opposites:              {len(report.antonym)}")
    print(f"  both spellings common:  {len(report.both_common)}")
    print(f"  invisible to users:     {len(report.not_user_facing)} (code identifiers only)")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, []))

    if verbose:
        for title, items in (
            ("Shape not typo-like", report.not_typo_shaped),
            ("One nested in another", report.nested),
            ("Digit-only difference", report.digit_only),
            ("Opposites", report.antonym),
            ("Both spellings common", report.both_common),
            ("Invisible to users", report.not_user_facing),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items[:40]:
                    print(f"  {i}")
                if len(items) > 40:
                    print(f"  ... and {len(items) - 40} more")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="One name spelled two ways")
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
                        "kind": f.kind, "shape": f.shape, "hard": f.hard,
                        "a": f.a, "b": f.b, "a_count": f.a_count, "b_count": f.b_count,
                        "a_ref": f.a_ref, "b_ref": f.b_ref, "detail": f.detail,
                    }
                    for f in report.findings
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(f.hard for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
