#!/usr/bin/env python3
"""buildprobe.py: can behaviour be checked on this project at all.

The fifth stage of the pipeline and the only one that opens a **new family of
checks** instead of improving the existing ones.

WHY. Every detector here reads text: sources, specs, changelogs, CI matrices.
None of them runs the project. Hence the ceiling: we see contradictions between
two written statements and none at all between a statement and **behaviour**.
Sanitizers, differential runs, property-based testing, mutation testing, that
whole family starts with one question: does anything build here, and with which
command.

The question sounds simple and the answer costs a lot. A sweep over 151 clones
showed that "a Go project" and "a project where `go build ./...` passes" are
different sets, and the second one is noticeably smaller.

WHAT IT DOES.

  1. Detects the build system by marker files: `go.mod`, `Cargo.toml`,
     `CMakeLists.txt`, `pyproject.toml`, `package.json`, `Makefile`.
  2. Names the command such a project is built and tested with, and checks
     **whether the required tool is installed**.
  3. On request, runs it with a timeout and reads the outcome.
  4. Looks for signs of readiness for dynamic checks: sanitizer targets, fuzzing,
     `-race`, coverage. A project that already runs these in CI is a place where
     such a report will be understood and reproduced.

WHY NOTHING RUNS BY DEFAULT. Building someone else's project means executing
someone else's code: `npm install` triggers `postinstall`, `make` does whatever
the rules say, `pip install -e .` executes `setup.py`. So the run is turned on
by an explicit `--run` flag, and without it the tool only looks and names the
command. The sweep walks a hundred foreign clones in a row, and "just have a
look" must never mean "execute a hundred unknown scripts".

WHAT THE TOOL DOES NOT DO:
  - **it does not fix builds.** A failed build is a fact about the project;
  - **it does not invent commands.** When the system is unrecognised it says so;
  - **it reports no defects.** Hard findings do not exist here at all: the
    question is "is this project usable", not "is there a defect". Its exit code
    is therefore always 0.

KNOWN BLIND SPOTS:
  - multi-module trees: it looks at the root and one level down;
  - builds that need keys, credentials, docker or hardware;
  - projects where the build command exists only as prose in the README.

Run:
  python3 buildprobe.py ~/Projects/oss/qdrant
  python3 buildprobe.py --parent ~/Projects/oss          # what is usable at all
  python3 buildprobe.py ~/Projects/oss/rclone --run      # actually build it

Tests: test_buildprobe.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402


@dataclass
class System:
    name: str
    marker: str            # marker file
    tool: str              # what has to be in PATH
    build: str
    test: str
    dynamic: str = ""      # how dynamic checks are run in this system


# Order matters: systems with an unambiguous marker come first, then Makefile,
# which sits next to anything and says nothing about the language by itself.
SYSTEMS: Tuple[System, ...] = (
    System("go", "go.mod", "go", "go build ./...", "go test ./...",
           "go test -race ./..."),
    System("rust", "Cargo.toml", "cargo", "cargo build", "cargo test",
           "cargo +nightly test -Zsanitizer=address"),
    System("cmake", "CMakeLists.txt", "cmake",
           "cmake -S . -B build && cmake --build build", "ctest --test-dir build",
           "cmake -DCMAKE_C_FLAGS=-fsanitize=address -S . -B build-asan"),
    System("python", "pyproject.toml", "python3", "python3 -m pip install -e .",
           "python3 -m pytest -q", "python3 -m pytest -q -p no:randomly"),
    System("python", "setup.py", "python3", "python3 -m pip install -e .",
           "python3 -m pytest -q"),
    System("node", "package.json", "npm", "npm ci", "npm test"),
    System("make", "Makefile", "make", "make", "make test"),
)

# Signs that a project already runs something dynamic. This is about whether a
# behaviour report will be understood there rather than about project quality.
DYNAMIC_MARKS: Dict[str, Tuple[str, ...]] = {
    "sanitizers": ("-fsanitize", "asan", "ubsan", "msan", "tsan", "AddressSanitizer"),
    "races": ("-race", "race detector", "ThreadSanitizer"),
    "fuzzing": ("fuzz", "libFuzzer", "afl-fuzz", "oss-fuzz", "cargo-fuzz"),
    "properties": ("hypothesis", "quickcheck", "proptest", "property-based"),
    # `--cov` (pytest-cov) was missed: the list knew only the long forms.
    "coverage": ("coverage", "codecov", "-coverprofile", "llvm-cov", "--cov"),
}

# Where the dynamic markers are looked for. CI beats a Makefile: whatever runs
# on every pull request there reproduces for us too.
DYNAMIC_PLACES = (".github/workflows", "Makefile", "CMakeLists.txt", "justfile", "Taskfile.yml")


@dataclass
class Probe:
    root: str
    name: str = ""
    system: Optional[System] = None
    depth: int = 0                       # 0 for the root, 1 for a subdirectory
    subdir: str = ""
    tool_present: bool = False
    dynamic: List[str] = dc_field(default_factory=list)
    ran: bool = False
    built: Optional[bool] = None
    seconds: float = 0.0
    tail: str = ""
    notes: List[str] = dc_field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not self.system:
            return "build system not recognised"
        if not self.tool_present:
            return f"{self.system.name}: `{self.system.tool}` is not installed"
        if not self.ran:
            return f"{self.system.name}: not attempted, command is `{self.system.build}`"
        if self.built:
            return f"{self.system.name}: builds in {self.seconds:.0f} s"
        return f"{self.system.name}: does NOT build"

    @property
    def usable(self) -> bool:
        """Whether the project is usable for behaviour checks."""
        return bool(self.system) and self.tool_present and self.built is not False


# --------------------------------------------------------------------------


def detect(root: str) -> Tuple[Optional[System], str]:
    """The build system: the root first, then one level down.

    One level rather than the whole tree, on purpose. Multi-module projects like
    karmada or Boost carry dozens of marker files, and the tool does not pick a
    "main" one among them: it reports what it found at the root and stops there.
    """
    for sysd in SYSTEMS:
        if os.path.exists(os.path.join(root, sysd.marker)):
            return sysd, ""
    try:
        subs = sorted(
            d for d in os.listdir(root)
            if os.path.isdir(os.path.join(root, d))
            and d not in common.SKIP_DIRS and not d.startswith(".")
        )
    except OSError:
        return None, ""
    for sub in subs:
        for sysd in SYSTEMS:
            if os.path.exists(os.path.join(root, sub, sysd.marker)):
                return sysd, sub
    return None, ""


def dynamic_marks(root: str) -> List[str]:
    """Which dynamic checks the project already runs itself."""
    found = set()
    for place in DYNAMIC_PLACES:
        full = os.path.join(root, place)
        files: List[str] = []
        if os.path.isdir(full):
            for dirpath, _dirs, names in common.walk(full):
                files.extend(os.path.join(dirpath, n) for n in names
                             if n.endswith((".yml", ".yaml")))
        elif os.path.isfile(full):
            files.append(full)
        for path in files[:60]:
            text = common.read_text(path).lower()
            if not text:
                continue
            for kind, marks in DYNAMIC_MARKS.items():
                if any(m.lower() in text for m in marks):
                    found.add(kind)
    return sorted(found)


def run_build(cmd: str, cwd: str, timeout: int = 900) -> Tuple[bool, float, str]:
    # shell=True is needed for the `&&` in the cmake command. Commands come only
    # from the SYSTEMS table above and never from input, otherwise this would be
    # a hole.
    import time as _t
    started = _t.monotonic()
    try:
        p = subprocess.run(
            cmd, cwd=cwd, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return False, float(timeout), f"did not finish within {timeout} s"
    except OSError as e:  # noqa: BLE001
        return False, _t.monotonic() - started, f"failed to start: {e}"
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    return p.returncode == 0, _t.monotonic() - started, out[-1200:]


def probe_one(root: str, run: bool = False, timeout: int = 900) -> Probe:
    pr = Probe(root=root, name=os.path.basename(root.rstrip("/")))
    if not os.path.isdir(root):
        pr.notes.append("no such directory")
        return pr

    sysd, sub = detect(root)
    pr.system, pr.subdir, pr.depth = sysd, sub, 1 if sub else 0
    if not sysd:
        pr.notes.append("no marker file at the root or one level down")
        return pr
    if sub:
        pr.notes.append(f"marker found in `{sub}/` rather than at the root")

    pr.tool_present = shutil.which(sysd.tool) is not None
    pr.dynamic = dynamic_marks(root)

    if not run:
        pr.notes.append("no run requested: without `--run` foreign code is not executed")
        return pr
    if not pr.tool_present:
        pr.notes.append(f"`{sysd.tool}` is not installed, nothing to run with")
        return pr

    where = os.path.join(root, sub) if sub else root
    pr.ran = True
    pr.built, pr.seconds, pr.tail = run_build(sysd.build, where, timeout)
    if not pr.built and pr.tail:
        pr.notes.append("tail of the output: " + pr.tail.splitlines()[-1][:150])
    return pr


# --------------------------------------------------------------------------


def print_report(probes: List[Probe], verbose: bool = False) -> None:
    usable = [p for p in probes if p.usable]
    unknown = [p for p in probes if not p.system]

    if len(probes) == 1:
        p = probes[0]
        print(f"\n=== Build probe: {p.name} ===")
        print(f"  verdict:                {p.verdict}")
        if p.system:
            print(f"  build:                  {p.system.build}")
            print(f"  tests:                  {p.system.test}")
            if p.system.dynamic:
                print(f"  dynamic:                {p.system.dynamic}")
        print(f"  project already runs:   {', '.join(p.dynamic) or 'nothing dynamic in sight'}")
        for n in p.notes:
            print(f"  {n}")
    else:
        print(f"\n=== Usable projects ({len(usable)} of {len(probes)}) ===")
        for p in sorted(usable, key=lambda x: (x.system.name if x.system else "", x.name)):
            marks = ", ".join(p.dynamic) or "-"
            print(f"  {p.name:<28} {p.system.name:<8} {marks}")
        if unknown and verbose:
            print(f"\n--- Build system not recognised ({len(unknown)}) ---")
            for p in unknown[:40]:
                print(f"  {p.name}")

    by_system: Dict[str, int] = {}
    for p in probes:
        if p.system:
            by_system[p.system.name] = by_system.get(p.system.name, 0) + 1

    print("\n=== Coverage ===")
    print(f"  projects looked at:     {len(probes)}")
    print(f"  system recognised:      {len(probes) - len(unknown)}")
    if by_system:
        print("  by system:              " + ", ".join(
            f"{k} {v}" for k, v in sorted(by_system.items(), key=lambda x: -x[1])))
    print(f"  with dynamic markers:   {sum(1 for p in probes if p.dynamic)}")
    print(f"  actually built:         {sum(1 for p in probes if p.ran)}"
          f" (successfully {sum(1 for p in probes if p.built)})")
    # The prober never has hard findings: it is about usability rather than
    # defects. The line is printed to honour the shared contract.
    print(common.findings_line(0, len(usable)))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Is this project usable for behaviour checks")
    ap.add_argument("root", nargs="?", default=".", help="project directory")
    ap.add_argument("--parent", help="directory of clones: walk all of them")
    ap.add_argument("--run", action="store_true", help="actually build it (executes foreign code)")
    ap.add_argument("--timeout", type=int, default=900)
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    if args.parent:
        roots = [
            os.path.join(args.parent, d) for d in sorted(os.listdir(args.parent))
            if os.path.isdir(os.path.join(args.parent, d))
        ]
    else:
        roots = [args.root]

    probes = [probe_one(r, args.run, args.timeout) for r in roots]
    print_report(probes, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "hard": False, "project": p.name, "root": p.root,
                        "system": p.system.name if p.system else "",
                        "build": p.system.build if p.system else "",
                        "test": p.system.test if p.system else "",
                        "subdir": p.subdir, "tool_present": p.tool_present,
                        "dynamic": p.dynamic, "usable": p.usable,
                        "built": p.built, "seconds": round(p.seconds, 1),
                        "verdict": p.verdict, "notes": p.notes,
                    }
                    for p in probes
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 0   # usability is no finding, there is nothing to fail the pipeline with


if __name__ == "__main__":
    sys.exit(main())
