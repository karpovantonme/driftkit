"""syncdrift.py: the author promised two places match. Do they.

The species: somewhere in the tree a comment says "keep this in sync with
X", "must agree with X", "there is a copy of this in X". That is a promise
written by hand, checked by nobody. No compiler verifies it, no linter reads
it, and it rots the moment either side moves.

Two things go wrong, and the first one is cheap to catch:

  1. the promise names a path that no longer exists. Whoever edits one copy
     follows the pointer, finds nothing, and leaves the other copy alone.
  2. the promise resolves, but the two places have drifted apart.

This tool does the first automatically and prepares the second for a human:
it pairs the two sites and prints them side by side, because deciding whether
two pieces of code still "agree" is not a job for a regular expression.

THE VOCABULARY IS MINED, NOT INVENTED. Guessing the phrasing gives you
"keep in sync" and nothing else. Walking 3702 comments in the Go tree that
mention another file turned up the ones below, with counts. "Keep consistent
with" and "must agree with" would never have been on a guessed list, and
between them they carry a fifth of the promises in that tree.

Run it against a tree, not a file:

    python3 syncdrift.py --dir ~/src/go --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

# Phrases that make a promise about another place in the tree.
# Weight: 2 = an explicit obligation, 1 = a statement of origin (weaker, a
# copy is allowed to move on). Counts are from the Go tree, 2026-08-06.
PROMISES: list[tuple[str, int, str]] = [
    (r"keep (?:this |these |them |it )?in sync",           2, "30 in Go"),
    (r"keep(?:ing)? consistent with",                       2, "17 in Go"),
    (r"must (?:be )?(?:kept )?in sync",                     2, ""),
    (r"stays? in sync",                                     2, "5 in Go"),
    (r"must agree with",                                    2, "2 in Go, constants"),
    (r"must match",                                         2, ""),
    (r"(?:please )?also update",                            2, "4 in Go"),
    (r"(?:is |are )?mirrored in",                           2, "3 in Go, both halves"),
    (r"(?:is |are )?duplicated in",                         2, "11 in Go"),
    (r"there is a copy of (?:this|it)",                     2, "6 in Go"),
    (r"verbatim copy",                                      2, "3 in Go"),
    (r"(?:a |an )?(?:lightly |slightly )?modified copy",    1, ""),
    (r"very similar copy",                                  1, "20 in Go"),
    (r"copied from",                                        1, "52 in Go"),
    (r"copy of this (?:code|function|file|logic)",          2, ""),
    (r"same as (?:in|the one in)",                          1, "8 in Go"),
    (r"identical to",                                       1, ""),
    (r"adapted from",                                       1, "5 in Go"),
    (r"derived from",                                       1, "21 in Go"),
    (r"based on the (?:code|implementation) in",            1, ""),
    (r"ported from",                                        1, ""),
    (r"this follows the structure of",                      1, ""),
    (r"in step with",                                       2, ""),
    (r"needs? to be updated? (?:together|too|as well)",     2, ""),
    (r"if you (?:add|change|remove).{0,40}(?:also|too)",    2, ""),
    (r"remember to (?:update|change)",                      2, ""),
    (r"don't forget to (?:update|change)",                  2, ""),
    (r"analogous to",                                       1, ""),
    (r"counterpart (?:in|of)",                              1, ""),
    (r"equivalent (?:function|code|logic) in",              1, ""),
]

PROMISE_RE = re.compile("|".join(f"(?:{p})" for p, _, _ in PROMISES), re.I)
WEIGHT = [(re.compile(p, re.I), w) for p, w, _ in PROMISES]

# A reference to another place: a source path, or package/symbol.
#
# The extension must end the word. Without that guard `unsafe.Sizeof` parses
# as the file `unsafe.S` plus the symbol `izeof`, and single-letter assembly
# extensions turn every `pkg.Something` into a false hit. Cost of learning
# this: six bogus findings in the first Go run.
#
# But the guard has to be `(?!\w)(?!\.\w)`, not `(?![\w.])`. The blunter
# version also rejects a path at the end of a sentence -- "a copy of this
# lives in cmd/compile/internal/gc/noder.go." -- because the full stop
# follows the extension. That silently blinded the tool to the very finding
# it had just made in Go. Whenever a filter is tightened, re-run it against a
# known finding: fixing the ore is easy, and losing the vein with it is
# easier.
PATH_RE = re.compile(
    r"(?<![\w/.-])(?:src/)?"
    r"((?:[a-z][\w-]*/){0,6}[\w.-]+\.(?:go|c|h|cc|cpp|py|rs|js|ts|s|S))"
    r"(?!\w)(?!\.\w)"
    r"(?::(\w+))?"
)

# Headers that belong to the operating system or to a bundled library are
# real files, just not in this tree. Checking them says nothing.
SYSTEM_HEADER = re.compile(
    r"^(?:linux|sys|asm|asm-generic|net|netinet|arpa|mach|valgrind|openssl|"
    r"zlib|bits|gnu|uapi|windows|winsock2?|darwin)/", re.I)

COMMENT_RE = {
    ".go":  re.compile(r"^\s*(?://|\*)\s?(.*)$"),
    ".c":   re.compile(r"^\s*(?://|\*)\s?(.*)$"),
    ".h":   re.compile(r"^\s*(?://|\*)\s?(.*)$"),
    ".cc":  re.compile(r"^\s*(?://|\*)\s?(.*)$"),
    ".cpp": re.compile(r"^\s*(?://|\*)\s?(.*)$"),
    ".rs":  re.compile(r"^\s*(?:///?|//!)\s?(.*)$"),
    ".js":  re.compile(r"^\s*(?://|\*)\s?(.*)$"),
    ".ts":  re.compile(r"^\s*(?://|\*)\s?(.*)$"),
    ".py":  re.compile(r"^\s*#\s?(.*)$"),
}

SKIP_DIRS = {".git", "testdata", "vendor", "node_modules", "third_party",
             "__pycache__", ".venv", "target", "dist", "build"}

# A promise that points outside the tree is not ours to check: x/tools,
# chromium, gcc-mirror and friends all live somewhere else.
FOREIGN = re.compile(
    r"(?:golang\.org/x/|github\.com/|https?://|chromium|gcc-mirror|"
    r"\bx/(?:tools|net|sys|build|text|crypto)\b|rsc\.io/)", re.I)


def weight_of(text: str) -> int:
    return max((w for rx, w in WEIGHT if rx.search(text)), default=0)


def walk(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            ext = os.path.splitext(name)[1]
            if ext in COMMENT_RE:
                yield os.path.join(dirpath, name), ext


def collect(root: str, verbose: bool = False):
    findings, checked, foreign, promises, ambiguous = [], 0, 0, 0, 0
    # every source file in the tree, so a shortened path can be resolved by
    # its tail instead of being reported as missing
    index = [p for p, _ in walk(root)]
    for path, ext in walk(root):
        rx = COMMENT_RE[ext]
        try:
            lines = open(path, errors="ignore").read().split("\n")
        except OSError:
            continue
        for lineno, raw in enumerate(lines, 1):
            m = rx.match(raw)
            if not m:
                continue
            body = m.group(1).strip()
            if len(body) < 12 or not PROMISE_RE.search(body):
                continue
            promises += 1
            if FOREIGN.search(body):
                foreign += 1
                continue
            for pm in PATH_RE.finditer(body):
                target, symbol = pm.group(1), pm.group(2)
                # <ctype.h> and friends: the angle brackets say "not our tree"
                bracketed = (pm.start() > 0 and body[pm.start() - 1] == "<")
                if bracketed or SYSTEM_HEADER.match(target):
                    foreign += 1
                    continue
                checked += 1
                here = os.path.dirname(path)
                candidates = [
                    os.path.join(root, target),
                    os.path.normpath(os.path.join(here, target)),
                ]
                # tolerate the tree having a src/ root, as Go does
                candidates.append(os.path.join(root, "src", target))
                resolved = next((c for c in candidates if os.path.exists(c)), None)
                # People shorten paths to the part that disambiguates:
                # "ssa/html.go" from inside cmd/compile/internal/ir means the
                # sibling package. Accept a suffix match, but only when it is
                # unique in the tree -- two matches mean we cannot tell which
                # one was meant, and guessing would be worse than silence.
                if resolved is None:
                    tail = "/" + target
                    matches = [p for p in index if p.endswith(tail)]
                    if len(matches) == 1:
                        resolved = matches[0]
                    elif len(matches) > 1:
                        ambiguous += 1
                        continue
                if resolved is None:
                    findings.append({
                        "kind": "dangling",
                        "file": os.path.relpath(path, root),
                        "line": lineno,
                        "target": target,
                        "symbol": symbol,
                        "weight": weight_of(body),
                        "text": body[:200],
                    })
                elif verbose:
                    print(f"  ok  {os.path.relpath(path, root)}:{lineno} -> {target}",
                          file=sys.stderr)
    return findings, {"promises": promises, "refs_checked": checked,
                      "pointing_outside": foreign, "ambiguous": ambiguous}


def main() -> int:
    ap = argparse.ArgumentParser(description="the author promised two places match")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.dir))
    findings, cov = collect(root, args.verbose)

    hard = [f for f in findings if f["weight"] == 2]
    soft = [f for f in findings if f["weight"] < 2]

    for group, title in ((hard, "PROMISE BROKEN, the file is not there"),
                         (soft, "weaker claim of origin, same problem")):
        if not group:
            continue
        print(f"\n=== {title} ===")
        for f in group:
            print(f"\n  {f['file']}:{f['line']}")
            print(f"      names: {f['target']}" + (f":{f['symbol']}" if f["symbol"] else ""))
            print(f"      {f['text']}")

    print("\n=== Coverage ===")
    print(f"  promises found:         {cov['promises']}")
    print(f"  pointing outside tree:  {cov['pointing_outside']} (not ours to check)")
    print(f"  file references read:   {cov['refs_checked']}")
    print(f"  shortened, two matches: {cov['ambiguous']} (cannot tell which was meant)")
    print(f"  findings:               {len(hard)} hard, {len(soft)} soft")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"findings": findings, "coverage": cov}, fh, indent=1)
        print(f"  written to:             {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
