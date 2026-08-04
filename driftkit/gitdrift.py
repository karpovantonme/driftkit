#!/usr/bin/env python3
"""gitdrift.py: a field was added, the function that walks the struct was not.

The species: a function goes through the fields of a struct by hand, copying,
comparing, merging or validating them, then a field gets added to the struct and
nobody updates the function. The field quietly stops being copied, compared or
validated.

Why this is worth catching and why no linter does: the code is formally correct.
The compiler is silent, tests are green, `go vet` is happy. It shows up only
through two sources at once, the field list of the struct and what the walker
actually touches, plus **dates**: the field landed after the last edit to the
function and nobody has opened it since.

The dates are what make a finding indisputable. Without them a maintainer can
fairly answer "we do not want that field copied". With dates the conversation is
different: the field appeared on this date, the function has not changed since.

The order:
  1. collect structs with enough fields;
  2. find functions touching a noticeable share of those fields, which means
     they walk the struct rather than merely mention it;
  3. list the fields such a function never touches;
  4. prove it by dates: find the commit that added the field and confirm the
     body of the function has not changed since.

WHAT THE TOOL DOES NOT CALL A FINDING, because it is normal:
  - fields nobody copies by design: mutexes, caches, loggers, counters,
    contexts, embedded types;
  - functions touching fewer than COVER_MIN of the fields: they do not walk the
    struct;
  - functions skipping more than MAX_MISS_SHARE of the fields: they walk a part
    of the struct on purpose, and an omission there carries meaning;
  - structs with fewer than MIN_FIELDS fields: too small to tell a walker from a
    coincidence.

Dependencies: an authenticated `gh` for the date proof. Without it the tool
still works and every finding is marked as unproven.

Run:
  python3 gitdrift.py --dir ~/Projects/oss/k8s/karmada/pkg --repo karmada-io/karmada
  python3 gitdrift.py --dir ... --no-proof     # offline and fast
  python3 gitdrift.py ... -v                   # list what was dismissed, by name

Tests: test_gitdrift.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stamp  # noqa: E402

import gosym  # noqa: E402
from liftdrift import GitHub, DEFAULT_CACHE  # noqa: E402

MIN_FIELDS = 5          # fewer than this cannot separate a walker from chance
COVER_MIN = 0.7         # below this share of fields a function does not walk the struct
MAX_MISS_SHARE = 0.3    # above this the function walks part of the struct on purpose
MIN_MIRRORED = 3        # this many fields have to be mirrored for a walker
MIN_ENUM_MEMBERS = 3    # a shorter enumeration cannot be told from a coincidence

# Fields nobody copies or compares by design. The list is deliberately generous:
# an extra omission makes the tool go quiet rather than invent something.
SKIP_FIELD = re.compile(
    r"^(?:mu|mtx|mutex|lock|rw|rwmu|wg|once|cache|logger|log|client|clientset|ctx|"
    r"cancel|stop|stopCh|ch|done|conn|db|tx|tracer|meter|metrics|recorder|"
    r"informer|lister|queue|scheme|codec|rest|restMapper|eventRecorder)$",
    re.I,
)


# Enumeration members deliberately skipped in a switch
SKIP_MEMBER = re.compile(
    r"^(?:_|\w*(?:Unknown|None|Invalid|Unspecified|Undefined|Nil|Max|Count|Last|Sentinel))$", re.I
)

# A generated file. A finding there is pointless: the generator is what needs
# fixing. In otelcol all twelve candidates sat in
# `generated_metrics.go`.
GENERATED = re.compile(r"^//\s*Code generated .*DO NOT EDIT", re.M | re.I)


@dataclass
class Finding:
    struct: str
    func: str
    field: str
    struct_ref: str
    func_ref: str
    field_ref: str
    covered: int
    total: int
    shape: str = "walker"
    proof: str = ""
    hard: bool = False
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    files: int = 0
    structs: int = 0
    walkers: int = 0
    enums: int = 0
    switches: int = 0
    small_enums: List[str] = dc_field(default_factory=list)
    generated: List[str] = dc_field(default_factory=list)
    has_default: List[str] = dc_field(default_factory=list)
    small_structs: List[str] = dc_field(default_factory=list)
    low_cover: List[str] = dc_field(default_factory=list)
    type_not_named: List[str] = dc_field(default_factory=list)
    too_many_missing: List[str] = dc_field(default_factory=list)
    skipped_fields: List[str] = dc_field(default_factory=list)
    unproven: List[str] = dc_field(default_factory=list)
    findings: List[Finding] = dc_field(default_factory=list)
    api_calls: int = 0
    cache_hits: int = 0


# --------------------------------------------------------------------------


# A mirrored reference to a field: `out.Foo = in.Foo`, `a.Foo == b.Foo`.
# The same field name on both sides is a reliable sign the function carries or
# compares the struct field by field rather than merely using it.
_MIRROR = re.compile(r"\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|==|!=)\s*[\w\)\]\*]+\.\s*\1\b")


def mirrored_fields(body: str) -> Set[str]:
    return {m.group(1) for m in _MIRROR.finditer(body)}


def _touches(body: str, field: str) -> bool:
    return re.search(r"\.\s*" + re.escape(field) + r"\b", body) is not None


def scan_file(path: str, src: str, report: Report) -> List[Finding]:
    if GENERATED.search(src[:4000]):
        report.generated.append(path)
        return []
    decls = gosym.declarations(src)
    clean = gosym.strip_code(src)
    clean_lines = clean.splitlines()
    funcs = [d for d in decls if d.kind == "func"]
    out: List[Finding] = []

    # --- second shape: an enumeration against a switch ---
    for type_name, members, decl_line in gosym.iota_enums(src):
        report.enums += 1
        names = [n for n, _ in members if not SKIP_MEMBER.match(n)]
        line_of_m = dict(members)
        if len(names) < MIN_ENUM_MEMBERS:
            report.small_enums.append(f"{type_name} ({os.path.basename(path)}:{decl_line}, {len(names)})")
            continue
        for fn in funcs:
            body = "\n".join(clean_lines[fn.start - 1 : fn.end])
            if "switch" not in body:
                continue
            cases = gosym.switch_cases(body)
            covered = [n for n in names if n in cases]
            if len(covered) < MIN_ENUM_MEMBERS or len(covered) / len(names) < COVER_MIN:
                continue
            missing = [n for n in names if n not in cases]
            if not missing:
                continue
            report.switches += 1
            # `default:` means the case is provided for and the omission is
            # deliberate. The `exhaustive` linter follows the same convention.
            has_default = re.search(r"^\s*default\s*:", body, re.M) is not None
            if has_default:
                report.has_default.append(
                    f"{type_name} <- {fn.name} ({os.path.basename(path)}:{fn.start}): has a default"
                )
                continue
            for n in missing:
                out.append(
                    Finding(
                        struct=type_name,
                        func=fn.name,
                        field=n,
                        struct_ref=f"{path}:{decl_line}",
                        func_ref=f"{path}:{fn.start}",
                        field_ref=f"{path}:{line_of_m[n]}",
                        covered=len(covered),
                        total=len(names),
                        shape="enumeration",
                    )
                )

    for d in decls:
        fields = gosym.struct_fields(d)
        if not fields:
            continue
        report.structs += 1
        # Fields nobody copies by design are dropped IMMEDIATELY, before any
        # counting. Otherwise a struct with a mutex and a logger gets filtered out
        # by the share of skipped fields and never judged at all.
        by_design = [n for n, _ in fields if SKIP_FIELD.match(n)]
        for n in by_design:
            report.skipped_fields.append(f"{d.name}.{n}: a field of this kind is not copied")
        fields = [(n, ln) for n, ln in fields if not SKIP_FIELD.match(n)]
        names = [n for n, _ in fields]
        if len(names) < MIN_FIELDS:
            report.small_structs.append(f"{d.name} ({os.path.basename(path)}:{d.start}, {len(names)} fields)")
            continue
        line_of = dict(fields)

        for fn in funcs:
            body = "\n".join(
                clean_lines[fn.start - 1 : fn.end]
            )  # body without strings and comments: a field name in text is no access
            # The function has to name the type itself, in the signature, in a
            # literal or in a conversion. Without this, sibling structs sharing
            # field names get counted for each other: in karmada ProviderInfo,
            # RegionInfo and ZoneInfo share five field names, and a function about
            # one of them produced findings in all three.
            if not re.search(r"\b" + re.escape(d.name) + r"\b", body):
                report.type_not_named.append(f"{d.name} <- {fn.name}: the function never names the type")
                continue
            # A walker mirrors fields rather than merely using them. The first
            # version counted any function touching 70% of the fields as a walker
            # and caught ordinary methods like Run: 35 candidates, nearly all
            # false.
            mirrored = mirrored_fields(body) & set(names)
            if len(mirrored) < MIN_MIRRORED:
                continue
            touched = [n for n in names if _touches(body, n)]
            cover = len(touched) / len(names)
            tag = f"{d.name} <- {fn.name} ({os.path.basename(path)}:{fn.start})"
            if cover < COVER_MIN:
                report.low_cover.append(f"{tag}: touches {len(touched)} of {len(names)}")
                continue
            report.walkers += 1
            missing = [n for n in names if n not in touched]
            if not missing:
                continue
            if len(missing) / len(names) > MAX_MISS_SHARE:
                report.too_many_missing.append(f"{tag}: skips {len(missing)} of {len(names)}")
                continue
            for n in missing:
                out.append(
                    Finding(
                        struct=d.name,
                        func=fn.name,
                        field=n,
                        struct_ref=f"{path}:{d.start}",
                        func_ref=f"{path}:{fn.start}",
                        field_ref=f"{path}:{line_of[n]}",
                        covered=len(touched),
                        total=len(names),
                    )
                )
    return out


def discover(root: str, report: Report) -> List[Finding]:
    out: List[Finding] = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("vendor", "testdata", ".git")]
        for n in sorted(names):
            if not n.endswith(".go") or n.endswith("_test.go"):
                continue
            p = os.path.join(dirpath, n)
            with open(p, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            report.files += 1
            out.extend(scan_file(p, src, report))
    return out


# --------------------------------------------------------------------------
# Proof by dates
# --------------------------------------------------------------------------


def prove(gh: GitHub, repo: str, repo_path: str, f: Finding, func_body: str) -> Optional[dict]:
    """Finds the commit that added a field and checks the function was untouched since.

    When the body of the function in that commit matches today, nobody has opened
    it. That is the proof: the field appeared and the walker was never updated.
    """
    commits = gh.api(f"repos/{repo}/commits?path={repo_path}&per_page=100", paginate=True)
    if not isinstance(commits, list):
        return None
    adder = None
    for c in commits:  # newest first, looking for the latest one that added the field
        if len((c.get("parents") or [])) != 1:
            continue
        detail = gh.api(f"repos/{repo}/commits/{c['sha']}")
        if not isinstance(detail, dict):
            continue
        for fl in detail.get("files") or []:
            if fl.get("filename") != repo_path:
                continue
            for ln in (fl.get("patch") or "").splitlines():
                if ln.startswith("+") and not ln.startswith("+++"):
                    if re.match(r"^\+\s*" + re.escape(f.field) + r"\s+\S", ln):
                        adder = c
                        break
            if adder:
                break
        if adder:
            break
    if adder is None:
        return None

    src_then = gh.raw_file(repo, repo_path, adder["sha"])
    if src_then is None:
        return None
    then = gosym.find(src_then, f.func)
    if then is None:
        return None
    if not gosym.bodies_equal(then.text, func_body, drop_qualifiers=False):
        return None  # the function was edited afterwards, the omission may be a decision
    return adder


# --------------------------------------------------------------------------


def analyse(root: str, repo: Optional[str], gh: Optional[GitHub], report: Report, repo_root: str = "") -> None:
    findings = discover(root, report)
    bodies: Dict[str, str] = {}
    for f in findings:
        path = f.func_ref.rsplit(":", 1)[0]
        if path not in bodies:
            with open(path, encoding="utf-8", errors="replace") as fh:
                bodies[path] = fh.read()
        if gh is None or not repo:
            report.unproven.append(f"{f.struct}.{f.field}: no date proof was looked for")
            findings_hard = False
        else:
            rel = os.path.relpath(path, root)
            repo_path = os.path.join(repo_root, rel) if repo_root else rel
            decl = gosym.find(bodies[path], f.func)
            c = prove(gh, repo, repo_path, f, decl.text if decl else "")
            if c is None:
                report.unproven.append(
                    f"{f.struct}.{f.field}: no commit adding the field after the last edit was found"
                )
                findings_hard = False
            else:
                f.proof = f"https://github.com/{repo}/commit/{c['sha']}"
                f.detail.append(
                    f"  field added on {c['commit']['committer']['date'][:10]}: "
                    f"{c['commit']['message'].splitlines()[0][:90]}"
                )
                findings_hard = True
        f.hard = findings_hard
    report.findings = findings
    if gh is not None:
        report.api_calls = gh.calls
        report.cache_hits = gh.cache_hits


def print_report(report: Report, verbose: bool = False) -> None:
    hard = [f for f in report.findings if f.hard]
    soft = [f for f in report.findings if not f.hard]

    def block(title: str, items: List[Finding]) -> None:
        if not items:
            return
        print(f"\n=== {title} ({len(items)}) ===")
        for f in items:
            print(f"\n[{f.struct}.{f.field}]")
            if f.shape == "enumeration":
                print(f"  {f.func} handles {f.covered} of {f.total} values of {f.struct}; this one is skipped and there is no default")
            else:
                print(f"  {f.func} touches {f.covered} of {f.total} fields of {f.struct} and never this one")
            print(f"  struct:   {f.struct_ref}")
            print(f"  field:    {f.field_ref}")
            print(f"  function: {f.func_ref}")
            if f.proof:
                print(f"  commit:   {f.proof}")
            for d in f.detail:
                print(f"  {d}")

    block("Field added, the walker never updated", hard)
    block("Needs reading by a human", soft)

    print("\n=== Coverage ===")
    print(f"  files read:             {report.files}")
    print(f"  structs found:          {report.structs}")
    print(f"  structs too small:      {len(report.small_structs)} (fewer than {MIN_FIELDS})")
    print(f"  walkers found:          {report.walkers}")
    print(f"  generated files:        {len(report.generated)} (the generator is what to fix)")
    print(f"  enumerations found:     {report.enums}")
    print(f"  enumerations too small: {len(report.small_enums)} (fewer than {MIN_ENUM_MEMBERS} values)")
    print(f"  switches over enums:    {report.switches}")
    print(f"  has a default:          {len(report.has_default)} (the omission is deliberate)")
    print(f"  type never named:       {len(report.type_not_named)} (siblings sharing fields)")
    print(f"  not a walker:           {len(report.low_cover)} (touches under {COVER_MIN:.0%} of fields)")
    print(f"  skips too much:         {len(report.too_many_missing)} (over {MAX_MISS_SHARE:.0%})")
    print(f"  never-copied fields:    {len(report.skipped_fields)}")
    print(f"  unproven by dates:      {len(report.unproven)}")
    print(f"  GitHub requests:        {report.api_calls} (from cache {report.cache_hits})")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, ['gosym.py']))

    if verbose:
        for title, items in (
            ("Type never named", report.type_not_named),
            ("Not a walker", report.low_cover),
            ("Skips too much", report.too_many_missing),
            ("Fields of a kind nobody copies", report.skipped_fields),
            ("Switch has a default", report.has_default),
            ("Unproven by dates", report.unproven),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items[:50]:
                    print(f"  {i}")
                if len(items) > 50:
                    print(f"  ... and {len(items) - 50} more")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="A field was added, the walker was not updated")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--repo", help="owner/name, needed for the date proof")
    ap.add_argument("--repo-root", default="", help="path of --dir inside the repository")
    ap.add_argument("--offline", "--no-proof", dest="no_proof", action="store_true",
                    help="stay offline; findings will remain soft")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    gh = None if args.no_proof else GitHub(args.cache)
    report = Report()
    analyse(args.dir, args.repo, gh, report, args.repo_root)
    print_report(report, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "struct": f.struct, "func": f.func, "field": f.field, "shape": f.shape,
                        "struct_ref": f.struct_ref, "field_ref": f.field_ref,
                        "func_ref": f.func_ref, "covered": f.covered, "total": f.total,
                        "hard": f.hard, "proof": f.proof, "detail": f.detail,
                    }
                    for f in report.findings
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(f.hard for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
