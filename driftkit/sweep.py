#!/usr/bin/env python3
"""sweep.py: one run over a project instead of eleven by hand.

The job is to **work out what this project allows checking at all** and to say
out loud what cannot be checked and why.

Why this is no small thing. The tools are invoked differently: `--proto-dir`
with `--openapi`, `--dir`, a pair of `--original` and `--translation`. Holding
that in your head is extra work, and forgetting one tool means missing a whole
species. The bigger danger is another one: **a run that quietly skipped half the
tools looks exactly like "this project is clean"**. Same mistake as "zero
findings out of zero compared", one level up.

Hence the order:

  1. **Survey.** Look at what the tree holds: .proto files with a REST
     description, lifted-code markers, translation directories, a changelog,
     helm-unittest suites, package metadata with a CI matrix, Go files.
  2. **Plan.** Print which checks apply, which do not and for what reason. The
     plan is printed ALWAYS, even with --dry-run.
  3. **Run.** Applicable checks only. Network proofs go behind an explicit
     --network flag, because they cost hundreds of requests.
  4. **Record.** One row per check in the registry: date, project, check,
     applicable, findings, tool fingerprint. So that a week later you can answer
     whether this project was checked with this tool and which version of it.

Run:
  python3 sweep.py --dir ~/Projects/oss/etcd
  python3 sweep.py --dir ... --dry-run     # plan only, run nothing
  python3 sweep.py --dir ... --network     # enable network proofs
  python3 sweep.py --dir ... --only deaddrift,namedrift

Tests: test_sweep.py next to this file.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import buildprobe  # noqa: E402
import common  # noqa: E402
import sitecheck  # noqa: E402
import stamp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REGISTRY = os.path.join(os.path.dirname(HERE), "check-registry.tsv")

SKIP_DIRS = common.SKIP_DIRS  # one list for the whole kit
LOCALE = re.compile(r"^[a-z]{2}(?:[-_][A-Za-z]{2})?$")
DEFAULT_LOCALES = ("en", "en-us", "english")


@dataclass
class Plan:
    tool: str
    applies: bool
    reason: str
    args: List[List[str]] = dc_field(default_factory=list)
    needs_network: bool = False


@dataclass
class Result:
    tool: str
    ran: bool
    reason: str
    hard: int = 0
    soft: int = 0
    output: str = ""
    fingerprint: str = ""


@dataclass
class Refuted:
    """The refutation result: how many findings were removed and why."""
    killed: int = 0
    unknown: int = 0
    left: int = 0
    reasons: Dict[str, int] = dc_field(default_factory=dict)
    ran: bool = False


# --------------------------------------------------------------------------
# Surveying the project
# --------------------------------------------------------------------------


def _walk(root: str):
    for dirpath, dirs, names in os.walk(root):
        # do not skip .github: CI matrices live there, and without them
        # supportdrift quietly declares a project unsuitable
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and (d == ".github" or not d.startswith("."))]
        yield dirpath, dirs, names


def survey(root: str) -> Dict[str, object]:
    """What the tree allows checking. One walk for every question."""
    s: Dict[str, object] = {
        "proto": [], "openapi": [], "go": 0, "changelog": [], "helm_tests": [],
        "lifted_links": 0, "locales": {}, "metadata": [], "workflows": False,
        "generated_spec": False, "text_files": 0, "py": 0, "hpp": 0,
        "js": 0,
    }
    for dirpath, _dirs, names in _walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel.endswith(os.sep + "workflows") or rel.endswith(".github/workflows"):
            s["workflows"] = True
        for n in names:
            low = n.lower()
            path = os.path.join(dirpath, n)
            if low.endswith(".proto"):
                s["proto"].append(path)  # type: ignore[union-attr]
            elif low in ("openapi.json", "swagger.json") or low.endswith(".swagger.json"):
                s["openapi"].append(path)  # type: ignore[union-attr]
                if low.endswith(".swagger.json"):
                    s["generated_spec"] = True
            elif low.endswith(".go"):
                s["go"] = int(s["go"]) + 1  # type: ignore[arg-type]
                if int(s["go"]) < 400:  # a sample this size is enough to count markers
                    try:
                        with open(path, encoding="utf-8", errors="replace") as fh:
                            s["lifted_links"] = int(s["lifted_links"]) + len(  # type: ignore[arg-type]
                                re.findall(r"https://github\.com/\S+/blob/\S+", fh.read())
                            )
                    except OSError:
                        pass
            elif re.match(r"^(changelog|changes|news|releases?)", low) and low.endswith((".md", ".rst", ".txt")):
                s["changelog"].append(path)  # type: ignore[union-attr]
            elif low.endswith(("_test.yaml", "_test.yml")) and os.path.basename(dirpath) == "tests":
                s["helm_tests"].append(path)  # type: ignore[union-attr]
            if low.endswith(".py"):
                s["py"] = int(s["py"]) + 1  # type: ignore[arg-type]
            elif low.endswith(".hpp"):
                s["hpp"] = int(s["hpp"]) + 1  # type: ignore[arg-type]
            elif low.endswith((".md", ".rst", ".txt", ".yaml", ".yml", ".c", ".h", ".py", ".rs", ".sh")):
                s["text_files"] = int(s["text_files"]) + 1  # type: ignore[arg-type]
            if low.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")):
                s["js"] = int(s["js"]) + 1  # type: ignore[arg-type]
            if n in ("pyproject.toml", "setup.py", "setup.cfg", "go.mod", "Cargo.toml", "package.json"):
                if dirpath == root:
                    s["metadata"].append(n)  # type: ignore[union-attr]
        if "buf.gen.yaml" in names or "protoc-gen-openapiv2" in _dirs_names(names):
            s["generated_spec"] = True

    s["locales"] = _find_locales(root)
    return s


def _dirs_names(names: Sequence[str]) -> str:
    return " ".join(names)


def _find_locales(root: str) -> Dict[str, Tuple[str, str]]:
    """Original and translation directory pairs: content/en against content/ja."""
    out: Dict[str, Tuple[str, str]] = {}
    for base in ("content", "docs", "i18n", "website/docs", "site/content"):
        d = os.path.join(root, base)
        if not os.path.isdir(d):
            continue
        subs = [x for x in sorted(os.listdir(d)) if os.path.isdir(os.path.join(d, x)) and LOCALE.match(x)]
        default = next((x for x in subs if x.lower() in DEFAULT_LOCALES), None)
        if not default:
            continue
        for x in subs:
            if x == default:
                continue
            src = os.path.join(d, default)
            dst = os.path.join(d, x)
            if glob.glob(os.path.join(dst, "**", "*.md"), recursive=True):
                out[f"{base}/{x}"] = (src, dst)
    return out


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def build_plan(root: str, s: Dict[str, object], network: bool) -> List[Plan]:
    plans: List[Plan] = []
    proto, openapi = s["proto"], s["openapi"]  # type: ignore[assignment]

    if proto and openapi:
        note = ""
        if s["generated_spec"]:
            note = (
                " BUT the spec appears to be generated from these same .proto "
                "files (grpc-gateway), so there can be no mismatch by construction "
                "and the run will be empty. This is our own reading of the project"
            )
        proto_dir = os.path.dirname(sorted(proto)[0])  # type: ignore[arg-type]
        plans.append(Plan("ifacedrift", True, f".proto files and {os.path.basename(sorted(openapi)[0])} are present{note}",
                          [["--proto-dir", proto_dir, "--openapi", sorted(openapi)[0]]]))  # type: ignore[index]
    else:
        why = "no .proto files" if not proto else "no openapi.json"
        plans.append(Plan("ifacedrift", False, why))

    n_links = int(s["lifted_links"])  # type: ignore[arg-type]
    if n_links >= 10:
        plans.append(Plan("liftdrift", True, f"{n_links} blob links in Go comments",
                          [["--dir", root] + ([] if network else ["--offline"])], needs_network=True))
    elif int(s["go"]) == 0:  # type: ignore[arg-type]
        plans.append(Plan("liftdrift", False, "not a Go project, the lifted-code parser reads Go only"))
    else:
        plans.append(Plan("liftdrift", False,
                          f"too few lifted-code markers with a file link ({n_links}), nothing to compare"))

    locales = s["locales"]  # type: ignore[assignment]
    if locales:
        args = [["--original", o, "--translation", t] + ([] if network else ["--offline"])
                for _k, (o, t) in sorted(locales.items())]  # type: ignore[union-attr]
        plans.append(Plan("transdrift", True, f"translation directories: {', '.join(sorted(locales))}",  # type: ignore[arg-type]
                          args, needs_network=True))
    else:
        plans.append(Plan("transdrift", False, "no translation directory next to an original"))

    if int(s["go"]) >= 10:  # type: ignore[arg-type]
        plans.append(Plan("gitdrift", True, f"{s['go']} Go files",
                          [["--dir", root] + ([] if network else ["--offline"])], needs_network=True))
    else:
        plans.append(Plan("gitdrift", False, "not a Go project, this tool parses Go only"))

    if s["metadata"] and s["workflows"]:
        plans.append(Plan("supportdrift", True, f"metadata {', '.join(s['metadata'])} and a CI matrix",  # type: ignore[arg-type]
                          [["--dir", root]]))
    else:
        why = "no package metadata" if not s["metadata"] else "no .github/workflows"
        plans.append(Plan("supportdrift", False, why))

    if int(s["text_files"]) >= 20:  # type: ignore[arg-type]
        plans.append(Plan("namedrift", True, f"{s['text_files']} text files", [["--dir", root]]))
    else:
        plans.append(Plan("namedrift", False, "too little text, no names to compare"))

    if s["changelog"]:
        plans.append(Plan("deaddrift", True, f"{len(s['changelog'])} changelogs",  # type: ignore[arg-type]
                          [["--dir", root]]))
    else:
        plans.append(Plan("deaddrift", False, "no changelog, so there is nowhere to take removed names from"))

    docs = int(s["text_files"])  # type: ignore[arg-type]
    if docs >= 10:
        plans.append(Plan("linkdrift", True, f"{docs} text files with links",
                          [["--dir", root] + ([] if network else ["--offline"])], needs_network=True))
    else:
        plans.append(Plan("linkdrift", False, "too few text files, no external links to take"))

    if int(s["py"]) >= 5:  # type: ignore[arg-type]
        plans.append(Plan("docdrift", True, f"{s['py']} Python files", [[root]]))
    else:
        plans.append(Plan("docdrift", False, "almost no Python files, no docstrings to compare"))

    if int(s["hpp"]) >= 5:  # type: ignore[arg-type]
        plans.append(Plan("doxdrift", True, f"{s['hpp']} .hpp headers", [[root]]))
    else:
        plans.append(Plan("doxdrift", False, "almost no .hpp headers"))

    if int(s["js"]) >= 5:  # type: ignore[arg-type]
        plans.append(Plan("paramdrift", True, f"{s['js']} JavaScript and TypeScript files", [[root]]))
    else:
        plans.append(Plan("paramdrift", False, "almost no JavaScript or TypeScript files"))

    if s["helm_tests"]:
        plans.append(Plan("assertdrift", True, f"{len(s['helm_tests'])} helm-unittest suites",  # type: ignore[arg-type]
                          [["--dir", root]]))
    else:
        plans.append(Plan("assertdrift", False, "no helm-unittest suites"))

    return plans


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------


def run_tool(plan: Plan, tmp_json: str, keep: Optional[List[dict]] = None) -> Result:
    hard = soft = 0
    chunks: List[str] = []
    for args in plan.args:
        cmd = [sys.executable, os.path.join(HERE, plan.tool + ".py"), *args, "--json", tmp_json]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        chunks.append(proc.stdout or proc.stderr)
        if os.path.exists(tmp_json):
            try:
                with open(tmp_json, encoding="utf-8") as fh:
                    items = json.load(fh)
                for x in items:
                    if x.get("hard", True):
                        hard += 1
                    else:
                        soft += 1
                if keep is not None:
                    # findings are collected whole: the refuter reads them
                    keep.extend(dict(x, tool=plan.tool) for x in items if isinstance(x, dict))
            except (OSError, json.JSONDecodeError):
                pass
            os.remove(tmp_json)
    return Result(
        tool=plan.tool, ran=True, reason=plan.reason, hard=hard, soft=soft,
        output="\n".join(chunks),
        fingerprint=stamp.fingerprint(os.path.join(HERE, plan.tool + ".py")),
    )


def sweep(
    root: str, network: bool, only: Optional[Sequence[str]], dry: bool,
    ignore_rules: bool = False, refute_after: bool = True,
) -> Tuple[List[Plan], List[Result], "sitecheck.Site", Refuted]:
    # First stage: is it worth bringing anything here at all. A project we may
    # not approach must not cost a single detector run.
    site = sitecheck.check(root, offline=not network)
    if site.blocked and not ignore_rules:
        return [], [], site, Refuted()
    s = survey(root)
    plans = build_plan(root, s, network)
    if only:
        plans = [p for p in plans if p.tool in only]
    results: List[Result] = []
    if dry:
        return plans, results, site, Refuted()
    tmp = os.path.join(HERE, ".sweep-tmp.json")
    found: List[dict] = []
    for p in plans:
        if not p.applies:
            results.append(Result(p.tool, False, p.reason))
            continue
        results.append(run_tool(p, tmp, found))

    # Third stage: refutation. Detectors hand their findings to the refuter
    # rather than to a human, and only what it fails to kill reaches the eye.
    ref = refute_findings(found, root) if (refute_after and found) else Refuted()
    return plans, results, site, ref


def refute_findings(found: List[dict], root: str) -> Refuted:
    """Runs `refute.py` over every finding of the project at once."""
    ref = Refuted()
    tmp_in = os.path.join(HERE, ".sweep-findings.json")
    tmp_out = os.path.join(HERE, ".sweep-survivors.json")
    try:
        with open(tmp_in, "w", encoding="utf-8") as fh:
            json.dump(found, fh, ensure_ascii=False)
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "refute.py"), "--findings", tmp_in,
             "--root", root, "--survivors", tmp_out],
            capture_output=True, text=True,
        )
        ref.ran = True
        for ln in (proc.stdout or "").splitlines():
            m = re.search(r"refuted:\s*(\d+)", ln)
            if m:
                ref.killed = int(m.group(1))
            m = re.search(r"without coordinates:\s*(\d+)", ln)
            if m:
                ref.unknown = int(m.group(1))
        if os.path.exists(tmp_out):
            with open(tmp_out, encoding="utf-8") as fh:
                ref.left = len(json.load(fh))
    except (OSError, json.JSONDecodeError, subprocess.SubprocessError):
        ref.ran = False
    finally:
        for f in (tmp_in, tmp_out):
            if os.path.exists(f):
                os.remove(f)
    return ref


# --------------------------------------------------------------------------


def write_registry(root: str, results: Sequence[Result], path: str = REGISTRY) -> None:
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("date\tproject\tcheck\tapplies\thard\tsoft\tfingerprint\treason\n")
        day = time.strftime("%Y-%m-%d")
        site = os.path.basename(os.path.abspath(root))
        for r in results:
            fh.write(
                f"{day}\t{site}\t{r.tool}\t{'yes' if r.ran else 'no'}\t"
                f"{r.hard if r.ran else ''}\t{r.soft if r.ran else ''}\t"
                f"{r.fingerprint}\t{r.reason}\n"
            )


def print_report(root: str, plans: Sequence[Plan], results: Sequence[Result],
                 verbose: bool, site: Optional["sitecheck.Site"] = None,
                 ref: Optional[Refuted] = None, registry: bool = True) -> None:
    print(f"\n=== Project: {os.path.abspath(root)} ===")
    if site is not None:
        print(f"  project verdict:      {site.verdict}")
        if site.rules.agent_trap:
            print(f"  trap in the template: {site.rules.agent_trap}, neither followed nor mentioned")
        if site.rules.dco:
            print("  DCO:                  git commit -s required")
        if site.blocked:
            print("\n  No detectors were run: we do not bring anything here.")
            return
    # The build prober only looks and runs nothing. It is here so that the
    # usability of the project for behaviour checks is visible right away.
    bp = buildprobe.probe_one(root, run=False)
    print(f"  build:                {bp.verdict}")
    if bp.dynamic:
        print(f"  project already runs: {', '.join(bp.dynamic)}")

    print("\n--- Plan ---")
    for p in plans:
        mark = "yes" if p.applies else "no "
        where = common.place_of(p.tool)
        # Where a check belongs is part of the plan, not a detail of the run.
        # A build check executes foreign code and wants a disposable runner;
        # saying so in the plan is what keeps it off a laptop by accident.
        tag = {"local": "", "network": "  [network]", "build": "  [online only]"}[where]
        net = " (needs the network for proof)" if p.applies and p.needs_network else ""
        print(f"  [{mark}] {p.tool:14s} {p.reason}{net}{tag}")

    online = [p.tool for p in plans if p.applies and common.place_of(p.tool) == common.BUILD]
    if online:
        print(f"\n  belongs on a runner rather than here: {', '.join(online)}")
        print(f"  limits for an online run: {common.LIMITS['targets_per_run']} projects, "
              f"{common.LIMITS['parallel_targets']} at a time, "
              f"{common.LIMITS['job_timeout_minutes']} min per job")

    if not results:
        print("\n(plan only, nothing was run)")
        return

    ran = [r for r in results if r.ran]
    print("\n--- Result ---")
    for r in results:
        if not r.ran:
            print(f"  [skipped] {r.tool:14s} {r.reason}")
        else:
            print(f"  [{r.hard:3d} hard / {r.soft:3d} soft] {r.tool:14s} fingerprint {r.fingerprint}")

    total_hard = sum(r.hard for r in ran)
    print(f"\n  checks applicable:      {len(ran)} of {len(results)}")
    print(f"  hard findings in total: {total_hard}")
    if ref is not None and ref.ran:
        print(f"  removed by refutation:  {ref.killed}")
        print(f"  left to read by hand:   {ref.left}")
        if ref.left:
            print("  next along the pipeline:")
            print("    probe.py     prove a finding about a test by mutation")
            print("    human eyes   everything mutation cannot reach")
            print("    lessons.py   after a maintainer replies, to turn it into a rule")
    # The line is printed only when the row will actually be written: a report
    # announcing something that did not happen is the same lie as an extra
    # finding, only harder to notice.
    if registry:
        print(f"  row appended to:        {os.path.basename(REGISTRY)}")
    if total_hard == 0 and ran:
        print("\n  Zero hard findings with a non-empty list of applicable checks is a")
        print("  result. What exactly was compared is visible in the reports below (-v).")

    if verbose:
        for r in ran:
            print(f"\n{'=' * 70}\n{r.tool}\n{'=' * 70}")
            print(r.output)


def find_sites(parent: str, depth: int = 2) -> List[str]:
    """Projects under a common directory. A repository is recognised by .git or
    by holding sources rather than only other directories."""
    out: List[str] = []

    def look(d: str, level: int) -> None:
        if level > depth:
            return
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        if ".git" in entries:
            out.append(d)
            return
        subs = [x for x in entries if os.path.isdir(os.path.join(d, x)) and not x.startswith(".")]
        files = [x for x in entries if os.path.isfile(os.path.join(d, x))]
        if files and level > 0:
            out.append(d)
            return
        for x in subs:
            look(os.path.join(d, x), level + 1)

    look(parent, 0)
    return out


def print_totals(rows: List[Tuple[str, List[Result]]]) -> None:
    print(f"\n{'=' * 78}\n=== Summary over {len(rows)} projects ===\n{'=' * 78}")
    by_tool: Dict[str, List[int]] = {}
    for _site, results in rows:
        for r in results:
            slot = by_tool.setdefault(r.tool, [0, 0, 0])  # applicable, hard, soft
            if r.ran:
                slot[0] += 1
                slot[1] += r.hard
                slot[2] += r.soft
    print(f"\n{'check':16s} {'projects':>9s} {'hard':>8s} {'soft':>7s}")
    for tool, (n, hard, soft) in sorted(by_tool.items(), key=lambda kv: -kv[1][1]):
        print(f"{tool:16s} {n:9d} {hard:8d} {soft:7d}")

    hot = sorted(((sum(r.hard for r in res), site) for site, res in rows), reverse=True)
    hot = [(h, s) for h, s in hot if h]
    if hot:
        print(f"\n--- Projects with hard findings ({len(hot)}) ---")
        for h, site in hot:
            print(f"  {h:4d}  {os.path.basename(site)}")
    else:
        print("\n  No hard findings on any project.")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="One run over a project instead of eleven by hand")
    ap.add_argument("--dir", help="a single project")
    ap.add_argument("--parent", help="directory of projects: walk all of them")
    ap.add_argument("--depth", type=int, default=2, help="how deep to look for projects under --parent")
    ap.add_argument("--network", action="store_true", help="enable network proofs (expensive)")
    ap.add_argument("--only", help="comma separated: which checks to run")
    ap.add_argument("--dry-run", action="store_true", help="plan only")
    ap.add_argument("--no-registry", action="store_true")
    ap.add_argument("--ignore-rules", action="store_true",
                    help="run detectors even where we would not bring anything")
    ap.add_argument("--no-refute", action="store_true",
                    help="skip the refuter after the detectors")
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true", help="print the full reports of every tool")
    args = ap.parse_args(argv)

    only = [x.strip() for x in args.only.split(",")] if args.only else None
    if not args.dir and not args.parent:
        ap.error("--dir or --parent is required")

    sites = [args.dir] if args.dir else find_sites(args.parent, args.depth)
    rows: List[Tuple[str, List[Result]]] = []
    blocked_sites: List[Tuple[str, str]] = []
    for site in sites:
        plans, results, scored, ref = sweep(
            site, args.network, only, args.dry_run, args.ignore_rules,
            refute_after=not args.no_refute,
        )
        if len(sites) == 1 or args.verbose:
            print_report(site, plans, results, args.verbose, scored, ref,
                         registry=bool(results) and not args.no_registry)
        elif scored.blocked:
            print(f"  {os.path.basename(site):32s} SKIPPED: {scored.verdict}")
            blocked_sites.append((site, scored.verdict))
        else:
            hard = sum(r.hard for r in results)
            ran = sum(1 for r in results if r.ran)
            tail = f"  after refutation {ref.left}" if ref.ran else ""
            print(f"  {os.path.basename(site):32s} checks {ran}/{len(results)}  hard {hard}{tail}")
        rows.append((site, results))
        if results and not args.no_registry:
            write_registry(site, results)
    if len(sites) > 1:
        print_totals(rows)
        if blocked_sites:
            print(f"\n--- Projects we do not bring anything to ({len(blocked_sites)}) ---")
            for site, why in blocked_sites:
                print(f"  {os.path.basename(site):32s} {why}")
    results = [r for _s, rs in rows for r in rs]
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "site": os.path.abspath(args.dir),
                    "plan": [{"tool": p.tool, "applies": p.applies, "reason": p.reason} for p in plans],
                    "results": [
                        {"tool": r.tool, "ran": r.ran, "hard": r.hard, "soft": r.soft,
                         "fingerprint": r.fingerprint, "reason": r.reason}
                        for r in results
                    ],
                },
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(r.hard for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
