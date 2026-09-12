#!/usr/bin/env python3
"""rstdrift.py: Sphinx and epytext parameter fields against the actual signature.

Same defect as docdrift, third and fourth dialect. `docdrift` parses numpydoc
and says so in its own blind spots; Google style has `gdrift`. Neither reads
the field form, and a large part of the ecosystem writes nothing else:

    :param str name: the function name to wrap        (Sphinx, PEP 287)
    @param socketfd: an AF_UNIX socket                (epytext, older code)

Pillow, SQLAlchemy, Twisted, Scrapy, certbot, salt, moto and everything
Zope-descended is written this way. Before this file nothing in the kit
looked at it.

The `@` form matters more than it looks. On Twisted the `:` form finds
**8** callables in the whole tree and the `@` form finds **2662**. A tool
reading only `:` would have called the project clean.

ONE CLASS OF FINDING, SPLIT BY WHAT THE SIGNATURE ALLOWS:

  hard: the name is in no `*`-form and simply is not an argument. The list of
    names comes from `ast`, so it is exact. Usually a rename the docstring
    did not follow.

  soft: the function takes `*args`, so a documented name may legitimately
    arrive through it. Pillow's `Image.eval(image, *args)` documents
    `:param function:` and the entry is the only hint the reader gets about
    what to pass second. Removing it would make the docs worse, so a human
    decides. `**kwargs` functions are skipped outright: there anything may
    be documented and there is nothing to weigh.

KNOWN BLIND SPOTS:
  - **the field form only.** numpydoc and Google style are not parsed here;
  - a class is judged by its `__init__`, and `@overload` stubs are skipped:
    the first stub carries a narrower signature than the implementation;
  - a class documenting its **attributes** under `:param:` reads as a finding
    and is not one. qutebrowser writes its whole codebase that way. There is
    no way to tell an attribute from an argument by the field alone;
  - `:param <type> <name>:` is read as name-last. `:param dict:` (a type with
    no name) therefore reports `dict`, which is the honest answer: Sphinx
    renders a parameter called `dict` there too;
  - a duplicated field name is invisible. Both names exist in the signature,
    so the check stays quiet. certbot had `:param bool default:` twice, the
    second meaning `ipv6`;
  - test, example and documentation directories are read like any other. The
    field form is rare enough in them that the noise has not been worth a
    filter yet.

Run:
  python3 rstdrift.py ~/src/certbot
  python3 rstdrift.py ~/src/salt --json out.json

Tests: test_rstdrift.py next to this file.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

# `:param name:`, `:param int name:`, `:arg name:`, `:keyword name:`, and the
# same set with `@` for epytext. The type, when present, comes first, so the
# name is the last word before the closing colon.
FIELD = re.compile(
    r"^[:@](?:param|parameter|arg|argument|keyword|key)\s+(?P<rest>[^:]+):",
)

COUNTS = {
    "files": 0,
    "unparsed": 0,
    "callables": 0,
    "kwargs_skipped": 0,
    "overload_skipped": 0,
}


def documented(doc: str) -> list[tuple[str, int]]:
    """Names in parameter fields, with the line offset inside the docstring."""
    out: list[tuple[str, int]] = []
    for i, raw in enumerate(doc.splitlines()):
        m = FIELD.match(raw.strip())
        if not m:
            continue
        name = m.group("rest").split()[-1].lstrip("*")
        if name.isidentifier():
            out.append((name, i))
    return out


def is_overload(fn) -> bool:
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name == "overload":
            return True
    return False


def declared(fn) -> tuple[set, bool, bool]:
    """Argument names, plus whether the signature has `*args` and `**kwargs`.

    `self` and `cls` stay in the set on purpose. Dropping them turns every
    docstring that documents them into a finding, and a module-level function
    whose real argument is called `cls` (decorators do this) into a false one.
    """
    a = fn.args
    names = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    star = a.vararg is not None
    if star:
        names.add(a.vararg.arg)
    star_star = a.kwarg is not None
    if star_star:
        names.add(a.kwarg.arg)
    return names, star, star_star


def walk(tree):
    """Yield (node holding the docstring, node whose signature counts)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, node
        elif isinstance(node, ast.ClassDef):
            inits = [
                b for b in node.body
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
                and b.name == "__init__"
            ]
            real = [b for b in inits if not is_overload(b)]
            if len(inits) > len(real):
                COUNTS["overload_skipped"] += len(inits) - len(real)
            if real:
                # The implementation is the last one; the stubs above it carry
                # narrower signatures and would produce false findings.
                yield node, real[-1]


def scan_file(path, repo):
    """A run over one file. Kept separate so the tests need no tree on disk."""
    hits = []
    try:
        tree = ast.parse(common.read_text(str(path)))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        COUNTS["unparsed"] += 1
        return hits
    COUNTS["files"] += 1
    for holder, sig in walk(tree):
        doc = ast.get_docstring(holder, clean=False)
        if not doc:
            continue
        names = documented(doc)
        if not names:
            continue
        COUNTS["callables"] += 1
        real, star, star_star = declared(sig)
        if star_star:
            COUNTS["kwargs_skipped"] += 1
            continue
        first = holder.body[0].lineno
        for name, off in names:
            if name in real:
                continue
            hits.append({
                "repo": repo,
                "path": str(path),
                "line": first + off,
                "symbol": f"{holder.name}()",
                "documented": name,
                "declared": ", ".join(sorted(real)) or "(none)",
                "hard": not star,
            })
    return hits


def scan(root):
    hits = []
    name = os.path.basename(os.path.abspath(root))
    for dirpath, _dirs, files in common.walk(root):
        for f in sorted(files):
            if f.endswith(".py"):
                hits.extend(scan_file(os.path.join(dirpath, f), name))
    return hits


def is_hard(hit: dict) -> bool:
    return bool(hit.get("hard", True))


def print_report(hits, name, verbose=False):
    hard = [h for h in hits if is_hard(h)]
    soft = [h for h in hits if not is_hard(h)]

    if hard:
        print(f"\n=== Documented name not in the signature ({len(hard)}) ===")
        for h in hard:
            print(f"\n  {h['path']}:{h['line']}  {h['symbol']}")
            print(f"    docstring:  {h['documented']}")
            print(f"    code:       {h['declared']}")

    if soft:
        print(f"\n=== Documented name absent, but the signature has *args ({len(soft)}) ===")
        for h in soft:
            print(f"\n  {h['path']}:{h['line']}  {h['symbol']}")
            print(f"    docstring:  {h['documented']}")
            print(f"    code:       {h['declared']}")
            print("    the name may arrive through *args; a human decides")

    print("\n=== Coverage ===")
    print(f"  tree:                   {name}")
    print(f"  files read:             {COUNTS['files']}")
    if COUNTS["unparsed"]:
        print(f"  files that failed to parse: {COUNTS['unparsed']}")
    print(f"  callables with a param field: {COUNTS['callables']}")
    if COUNTS["kwargs_skipped"]:
        print(f"  skipped (**kwargs):     {COUNTS['kwargs_skipped']}")
    if verbose and COUNTS["overload_skipped"]:
        print(f"  @overload stubs passed over: {COUNTS['overload_skipped']}")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Sphinx :param: and epytext @param fields against the signature")
    ap.add_argument("root", help="project directory")
    ap.add_argument("name", nargs="?", help="project name for the report")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    hits = scan(args.root)
    print_report(hits, args.name or args.root, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [dict(h, hard=is_hard(h)) for h in hits],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(is_hard(h) for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
