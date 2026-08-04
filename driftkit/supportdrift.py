#!/usr/bin/env python3
"""supportdrift.py: declared support against what CI actually runs.

Three sources that are supposed to agree and almost never do. Package metadata
promises language versions, the CI matrix runs different ones, and the README
names a third set.

A finding fits in one line and needs no decision: "Python 3.9 is declared as
supported, the matrix does not have it". Same family as a link to something that
no longer exists. A maintainer checks it in seconds.

Where the claim comes from:
  - `pyproject.toml`, `requires-python` and the `Programming Language` classifiers
  - `setup.py`, `setup.cfg` - `python_requires`
  - `go.mod`, the `go` directive
  - `Cargo.toml` - `rust-version`
  - `package.json` - `engines.node`

Where the check comes from: matrices in `.github/workflows/*.yml`, keys
`python-version`, `go-version`, `node-version`, `toolchain`, `rust`.

WHAT THE TOOL DOES NOT CALL A FINDING:
  - versions written in the matrix as an expression that cannot be expanded
    statically (`${{ matrix.x }}`, `stable`, `latest`, `oldstable`). No verdict
    can be given about such a matrix, and the report says so out loud;
  - a project with no matrix at all: there is nothing to compare against;
  - "the matrix has a version below the declared minimum" is ordinary backward
    compatibility testing. It goes into a separate soft line.

Run:
  python3 supportdrift.py --dir ~/Projects/oss/supabase-py
  python3 supportdrift.py --dir ... -v

Tests: test_supportdrift.py next to this file.
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
from typing import Dict, List, Optional, Sequence, Set, Tuple

# Values that cannot be expanded statically
NO_MINIMUM_CHECK = frozenset({"go"})

OPAQUE = re.compile(r"^\s*(\$\{\{|stable|latest|oldstable|nightly|beta|\*|current|lts)", re.I)


@dataclass
class Claim:
    language: str
    kind: str  # min | list
    versions: List[str]
    source: str  # file:line


@dataclass
class Finding:
    kind: str
    hard: bool
    language: str
    message: str
    claim_ref: str
    ci_ref: str
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    claims: List[Claim] = dc_field(default_factory=list)
    matrices: List[Tuple[str, str, List[str]]] = dc_field(default_factory=list)
    opaque: List[str] = dc_field(default_factory=list)
    no_matrix: List[str] = dc_field(default_factory=list)
    below_min: List[str] = dc_field(default_factory=list)
    incomplete_langs: Set[str] = dc_field(default_factory=set)
    findings: List[Finding] = dc_field(default_factory=list)


# --------------------------------------------------------------------------


def vkey(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3]) or (0,)


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _line_of(text: str, needle: str) -> int:
    for i, ln in enumerate(text.splitlines(), 1):
        if needle in ln:
            return i
    return 1


def collect_claims(root: str) -> List[Claim]:
    out: List[Claim] = []

    p = os.path.join(root, "pyproject.toml")
    if os.path.isfile(p):
        text = _read(p)
        m = re.search(r'requires-python\s*=\s*["\']([^"\']+)', text)
        if m:
            out.append(Claim("python", "min", _min_from_spec(m.group(1)), f"{p}:{_line_of(text, m.group(0))}"))
        cls = re.findall(r'Programming Language :: Python :: (\d+\.\d+)', text)
        if cls:
            out.append(Claim("python", "list", sorted(set(cls), key=vkey), f"{p}:{_line_of(text, 'Programming Language')}"))

    for name in ("setup.py", "setup.cfg"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            text = _read(p)
            m = re.search(r'python_requires\s*=\s*["\']?([^"\'\n,]+)', text)
            if m:
                out.append(Claim("python", "min", _min_from_spec(m.group(1)), f"{p}:{_line_of(text, m.group(0))}"))

    p = os.path.join(root, "go.mod")
    if os.path.isfile(p):
        text = _read(p)
        m = re.search(r"^go\s+(\d+\.\d+(?:\.\d+)?)", text, re.M)
        if m:
            out.append(Claim("go", "min", [m.group(1)], f"{p}:{_line_of(text, m.group(0))}"))

    p = os.path.join(root, "Cargo.toml")
    if os.path.isfile(p):
        text = _read(p)
        m = re.search(r'rust-version\s*=\s*["\']([^"\']+)', text)
        if m:
            out.append(Claim("rust", "min", [m.group(1)], f"{p}:{_line_of(text, m.group(0))}"))

    p = os.path.join(root, "package.json")
    if os.path.isfile(p):
        text = _read(p)
        try:
            data = json.loads(text)
            node = (data.get("engines") or {}).get("node")
            if node:
                out.append(Claim("node", "min", _min_from_spec(node), f"{p}:{_line_of(text, 'engines')}"))
        except json.JSONDecodeError:
            pass
    return out


def _min_from_spec(spec: str) -> List[str]:
    """Pulls 3.9 out of `>=3.9,<4`. The upper bound stays untouched: it forbids."""
    m = re.search(r">=?\s*v?(\d+\.\d+(?:\.\d+)?)", spec)
    if m:
        return [m.group(1)]
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", spec)
    return [m.group(1)] if m else []


_MATRIX_KEYS = {
    "python-version": "python",
    "python": "python",
    "go-version": "go",
    "go": "go",
    "node-version": "node",
    "node": "node",
    "toolchain": "rust",
    "rust": "rust",
    "rust-version": "rust",
}


def collect_matrices(root: str, report: Report) -> Dict[str, Set[str]]:
    """Versions from workflow matrices. Parsed with yaml when available, line by line otherwise."""
    found: Dict[str, Set[str]] = {}
    seen_keys: Set[str] = set()
    pending_opaque: List[Tuple[str, str, str]] = []  # (language, what the value was, where)
    wf_dir = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf_dir):
        return found
    for name in sorted(os.listdir(wf_dir)):
        if not name.endswith((".yml", ".yaml")):
            continue
        path = os.path.join(wf_dir, name)
        text = _read(path)
        for key, lang in _MATRIX_KEYS.items():
            for m in re.finditer(
                r"^\s*" + re.escape(key) + r"\s*:\s*(\[[^\]]*\]|\S.*)$", text, re.M
            ):
                raw = m.group(1).strip()
                if OPAQUE.match(raw):
                    pending_opaque.append((lang, raw, f"{path}:{_line_of(text, m.group(0))} - {key}: {raw[:50]}"))
                    continue
                vals = re.findall(r"['\"]?(\d+\.\d+(?:\.\d+)?)['\"]?", raw)
                if not vals:
                    continue
                found.setdefault(lang, set()).update(vals)
                report.matrices.append((path, lang, vals))
            # a list written under the key one item per line
            for m in re.finditer(
                r"^([ \t]*)" + re.escape(key) + r"\s*:\s*$\n((?:\1[ \t]+-\s*\S+\n?)+)", text, re.M
            ):
                vals = re.findall(r"-\s*['\"]?(\d+\.\d+(?:\.\d+)?)", m.group(2))
                if vals:
                    found.setdefault(lang, set()).update(vals)
                    report.matrices.append((path, lang, vals))
                    seen_keys.add(key)
            if re.search(r"^\s*" + re.escape(key) + r"\s*:\s*\[", text, re.M):
                seen_keys.add(key)

    # A reference to a matrix key we have already parsed is harmless:
    # `${{ matrix.python-version }}` is the same matrix we read. But
    # `${{ env.MIN_PYTHON }}`, `stable` and `latest` cannot be expanded, and then
    # we have no right to claim "this version is not tested": our list of versions
    # is knowingly incomplete. That is how nilearn hides its real matrix behind
    # env, and without this rule the tool invented two findings there.
    for lang, raw, where in pending_opaque:
        mm = re.match(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)", raw)
        if mm and mm.group(1) in seen_keys:
            continue
        report.opaque.append(where)
        report.incomplete_langs.add(lang)
    return found


# --------------------------------------------------------------------------


def analyse(root: str, report: Report) -> None:
    report.claims = collect_claims(root)
    tested = collect_matrices(root, report)

    for c in report.claims:
        if not c.versions:
            continue
        have = tested.get(c.language)
        if not have:
            report.no_matrix.append(f"{c.language}: declared in {c.source}, no CI matrix at all")
            continue
        ci_ref = next(
            (f"{p}" for p, lang, _ in report.matrices if lang == c.language), ".github/workflows"
        )
        if c.language in report.incomplete_langs:
            report.no_matrix.append(
                f"{c.language}: the matrix holds values that cannot be expanded, "
                f"so 'not tested' cannot be claimed ({c.source})"
            )
            continue
        if c.kind == "list":
            missing = [v for v in c.versions if v not in have]
            if missing:
                report.findings.append(
                    Finding(
                        kind="declared-not-tested",
                        hard=True,
                        language=c.language,
                        message=(
                            f"{c.language} support is declared for "
                            + ", ".join(missing)
                            + ", and the CI matrix does not have those versions"
                        ),
                        claim_ref=c.source,
                        ci_ref=ci_ref,
                        detail=[f"  in the matrix: {', '.join(sorted(have, key=vkey))}"],
                    )
                )
        else:
            lo = min(c.versions, key=vkey)
            below = [v for v in have if vkey(v) < vkey(lo)]
            if below:
                report.below_min.append(
                    f"{c.language}: minimum {lo} per {c.source}, CI also runs {', '.join(sorted(below, key=vkey))}"
                )
            # Go is deliberately excluded. The `go` directive in go.mod sets the
            # minimum language version; building with a newer toolchain is normal
            # practice there, and the directive promises nothing about CI. Three
            # "findings" on prometheus, thanos and argo-cd were exactly this.
            # A patch-level difference is no mismatch either: we compare minors.
            minor = lambda v: vkey(v)[:2]
            if c.language not in NO_MINIMUM_CHECK and not any(
                minor(v) == minor(lo) for v in have
            ):
                report.findings.append(
                    Finding(
                        kind="minimum-not-tested",
                        hard=False,
                        language=c.language,
                        message=(
                            f"the declared minimum {c.language} {lo} is never exercised by the CI matrix"
                        ),
                        claim_ref=c.source,
                        ci_ref=ci_ref,
                        detail=[f"  in the matrix: {', '.join(sorted(have, key=vkey))}"],
                    )
                )


def print_report(report: Report, verbose: bool = False) -> None:
    hard = [f for f in report.findings if f.hard]
    soft = [f for f in report.findings if not f.hard]
    for title, group in (("Declared and never exercised", hard), ("Needs reading by a human", soft)):
        if not group:
            continue
        print(f"\n=== {title} ({len(group)}) ===")
        for f in group:
            print(f"\n[{f.kind}] {f.language}")
            print(f"  {f.message}")
            print(f"  declared: {f.claim_ref}")
            print(f"  matrix:   {f.ci_ref}")
            for d in f.detail:
                print(f"  {d}")

    print("\n=== Coverage ===")
    print(f"  claims found:           {len(report.claims)}")
    for c in report.claims:
        print(f"    {c.language} {c.kind}: {', '.join(c.versions) or '-'} ({c.source})")
    print(f"  matrices found:         {len(report.matrices)}")
    print(f"  values not expandable:  {len(report.opaque)} (stable, latest, expressions)")
    print(f"  declared, no matrix:    {len(report.no_matrix)}")
    print(f"  CI below the minimum:   {len(report.below_min)} (backward compatibility, no defect)")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, []))

    if verbose:
        for title, items in (
            ("Values that cannot be expanded", report.opaque),
            ("Declared with no matrix", report.no_matrix),
            ("CI runs versions below the minimum", report.below_min),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items[:40]:
                    print(f"  {i}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Declared support against the CI matrix")
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
                        "kind": f.kind, "hard": f.hard, "language": f.language, "message": f.message,
                        "claim": f.claim_ref, "ci": f.ci_ref, "detail": f.detail,
                    }
                    for f in report.findings
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(f.hard for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
