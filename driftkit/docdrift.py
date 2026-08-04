#!/usr/bin/env python3
"""docdrift.py — numpydoc docstrings against the actual signature.

Two statements about the same thing disagree, and both live in the same
file, usually in the same declaration. That is what makes the fix obvious
to a maintainer and the review short. This is the check that produced the
most merged pull requests in this toolkit.

TWO CLASSES OF FINDING:

  A. **the Parameters section documents an argument the signature does not
     have.** Usually a rename: the code changed, the docstring did not.
     Unambiguous, because the signature is parsed with `ast` rather than a
     regex. Reported as hard.

  B. **the docstring states a default that differs from the real one.**
     Also compared against `ast`, but this compares free TEXT from the
     docstring against a LITERAL from the code, and the text is often prose:
     "default: None or 'auto'", "defaults to the value of ``n_jobs``".
     Reported as soft: a human decides.

The hard/soft split reflects how each check is built, not a measured
confirmation rate. There is no such measurement: findings from this check
have so far been read one by one.

KNOWN BLIND SPOTS:
  - **numpydoc only.** Google style (`Args:`) is not parsed at all: indentation
    carries less meaning there and the parse gets noisy;
  - functions with `*args`/`**kwargs` are skipped for class A, since anything
    may legitimately be documented for them;
  - defaults computed by an expression rather than written as a literal are
    skipped: `ast.literal_eval` stays quiet on those, which is correct;
  - test, example and documentation directories are not read: docstrings there
    are written to illustrate, not to describe an API.

Run:
  python3 docdrift.py ~/src/nilearn
  python3 docdrift.py ~/src/mne-python --json out.json

Tests: test_docdrift.py next to this file.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

# The shared skip list plus what is specific to Python projects: docstrings in
# tests, examples and docs are written to illustrate, not to describe an API.
SKIP_DIRS = common.SKIP_DIRS | {
    "test", "tests", "testing", "_test", "doc", "docs", "examples", "benchmarks",
    ".tox",
}

SEC = re.compile(r'^\s*(Parameters|Other Parameters|Keyword Arguments)\s*$')
UNDER = re.compile(r'^\s*-{3,}\s*$')
ANYSEC = re.compile(r'^\s*(Returns?|Yields?|Raises|See Also|Notes?|Examples?|References?|Attributes|Warns|Warnings|Methods|Other Parameters)\s*$')
PARAMLINE = re.compile(r'^(?P<ind>\s*)(?P<names>[*\w][\w\s,*]*?)\s*:\s*(?P<type>.+?)\s*$')
# A dot used to terminate the value, so `default 0.01` was read as `0` and
# every fractional default became a mismatch. The value now ends at a sentence
# boundary (". ") or at a comma, not at the first dot. Same family of mistake
# as a flag name truncated mid-word.
DEFAULT_IN_TYPE = re.compile(
    r'(?:default[\s:=]+|defaults to\s+)(?P<val>[^,;)\]]+?)(?=\.\s|[,;)\]]|$)', re.I)
EXAMPLES = re.compile(r'^\s*Examples?\s*$')
# Notes inside a Parameters section parse as arguments: the line
# `TODO: looks like not used yet` is shaped exactly like `name : type`.
# Seen in the wild in statsmodels, `gradient_momcond`.
NOTE_WORDS = frozenset({"TODO", "FIXME", "XXX", "HACK", "NOTE", "NB", "BUG", "WARNING"})


def cut_at_examples(lines):
    """Drop everything from the Examples section onwards.

    A doctest often prints someone else's docstring in full, and the printed
    output carries no indentation, so a `Parameters` section inside example
    output is indistinguishable from a real one. That is how mne's
    `copy_function_doc_to_method_doc` acquired arguments `a` and `b` that do
    not exist. numpydoc has no sections after Examples, so the rule is exact.
    """
    for i in range(len(lines) - 1):
        if EXAMPLES.match(lines[i]) and UNDER.match(lines[i + 1]):
            return lines[:i]
    return lines


def doc_params(doc):
    """-> [(names, type string, line number inside the docstring)]"""
    lines = cut_at_examples(doc.split('\n'))
    out, i = [], 0
    while i < len(lines) - 1:
        if SEC.match(lines[i]) and UNDER.match(lines[i + 1]):
            base_ind = None
            j = i + 2
            while j < len(lines):
                ln = lines[j]
                if ANYSEC.match(ln) and j + 1 < len(lines) and UNDER.match(lines[j + 1]):
                    break
                m = PARAMLINE.match(ln)
                if m and ln.strip():
                    ind = len(m.group('ind'))
                    if base_ind is None:
                        base_ind = ind
                    if ind == base_ind:
                        names = [n.strip().lstrip('*') for n in m.group('names').split(',')]
                        names = [n for n in names
                                 if re.fullmatch(r'\w+', n) and n not in NOTE_WORDS]
                        if names:
                            out.append((names, m.group('type'), j))
                j += 1
            i = j
        else:
            i += 1
    return out


# Compatibility decorators accept names beyond the ones in the signature.
# `@deprecate_kwarg("random_state", "rng")` in statsmodels means the function
# still takes `random_state`, and the docstring honestly documents both names.
# The signature says nothing about it, so a scanner reading only the signature
# calls a live name non-existent. Thirty false findings out of fifty six.
COMPAT_DEC = re.compile(r"deprecat|renam|alias|legacy|compat|moved|old_?name", re.I)


def _dec_label(dec) -> str:
    """Full decorator name including dots: `np.deprecate_kwarg`."""
    node = dec.func if isinstance(dec, ast.Call) else dec
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def compat_names(fn) -> Tuple[set, bool]:
    """Names accepted beyond the signature, plus a flag for not knowing.

    Returns (names, blind). "Blind" means a compatibility decorator is present
    but which names it adds cannot be read from the code. Class A is then not
    judged for that function at all: **you cannot claim "this name does not
    exist" while knowing the source only partially.** Same family of mistake
    as a CI matrix written as `${{ env.MIN_PYTHON }}` in supportdrift.
    """
    names: set = set()
    blind = False
    for dec in fn.decorator_list:
        if not COMPAT_DEC.search(_dec_label(dec)):
            continue
        if not isinstance(dec, ast.Call):
            blind = True          # bare @deprecated with no arguments
            continue
        found = False
        for a in dec.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                names.add(a.value)
                found = True
            elif isinstance(a, ast.Dict):          # @renamed({"old": "new"})
                for k in a.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        names.add(k.value)
                        found = True
        for kw in dec.keywords:
            if kw.arg:                              # @deprecated_alias(old="new")
                names.add(kw.arg)
                found = True
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                names.add(kw.value.value)
                found = True
        if not found:
            blind = True
    return names, blind


def is_property(fn) -> bool:
    """Is this a property (property, cached_property, functools.cached_property)."""
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        name = getattr(node, "attr", None) or getattr(node, "id", "") or ""
        if "property" in str(name):
            return True
    return False


def sig_params(fn):
    a = fn.args
    names, defaults = [], {}
    pos = list(a.posonlyargs) + list(a.args)
    for p in pos:
        names.append(p.arg)
    for p, d in zip(pos[len(pos) - len(a.defaults):], a.defaults):
        defaults[p.arg] = d
    for p in a.kwonlyargs:
        names.append(p.arg)
    for p, d in zip(a.kwonlyargs, a.kw_defaults):
        if d is not None:
            defaults[p.arg] = d
    star = bool(a.vararg or a.kwarg)
    if a.vararg: names.append(a.vararg.arg)
    if a.kwarg: names.append(a.kwarg.arg)
    return names, defaults, star


def lit(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return ...


def as_number(x):
    """A number from any notation: `1e-8`, `0o775`, `0x1f`, `1.0e-6`.

    pyTMD documents file permissions in octal as `0o775` while `literal_eval`
    returns decimal `509` from the code. Same value, so it cannot be a
    mismatch. Booleans are NOT treated as numbers, otherwise `True` would
    match `1`, and those are different defaults.
    """
    s = str(x).strip().strip("'\"")
    for conv in (ast.literal_eval, float):
        try:
            v = conv(s)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            return float(v)
    return None


def same_number(a, b) -> bool:
    va, vb = as_number(a), as_number(b)
    return va is not None and vb is not None and va == vb


def is_prose(val: str) -> bool:
    """Does the documented default read as prose rather than a value.

    Docstrings routinely state meaning instead of a literal: "default: all
    nodes in G", "default: len(G)", "default: first node in list(G)".
    Comparing that against a literal from the code is pointless.
    """
    v = val.strip()
    return bool(not v or " " in v or v.count("(") != v.count(")"))


def norm(v):
    s = str(v).strip().strip('`"\'').rstrip('.')
    return {'true': 'True', 'false': 'False', 'none': 'None'}.get(s.lower(), s)


def scan_file(path, repo):
    try:
        src = path.read_text(encoding='utf-8', errors='ignore')
        tree = ast.parse(src)
    except Exception:
        return []
    res = []
    parents = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            parents[c] = n
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(fn, clean=False)
        if not doc:
            continue
        # For a property the docstring describes not the function itself but
        # the callable it RETURNS. In networkx `G.edges` is a cached_property
        # returning a view that is called as `G.edges(nbunch, data)`; nbunch
        # and data belong in the docstring and cannot be in the signature.
        # 41 of the 44 hard findings on networkx were this.
        if is_property(fn):
            COUNTS["props"] += 1
            continue
        dp = doc_params(doc)
        if not dp:
            continue
        COUNTS["funcs"] += 1
        names, defaults, star = sig_params(fn)
        extra, blind = compat_names(fn)
        if blind:
            COUNTS["compat_blind"] += 1
        nameset = set(names) | extra | {'self', 'cls', 'kwargs', 'args'}
        for docnames, typ, _ in dp:
            for dn in docnames:
                if dn not in nameset and not star and not blind:
                    res.append(dict(kind='A', repo=repo, file=str(path), line=fn.lineno,
                                    func=fn.name, param=dn, sig=names, type=typ.strip()[:70]))
            m = DEFAULT_IN_TYPE.search(typ)
            if m:
                for dn in docnames:
                    if dn in defaults:
                        actual = lit(defaults[dn])
                        if actual is ...:
                            continue
                        want = norm(m.group('val'))
                        got = norm(repr(actual) if isinstance(actual, str) else actual)
                        # Sentinel `None`: the code says None, the docstring
                        # says what it turns into ("default: nx.Graph"). A
                        # language-wide idiom, not a defect. On networkx that
                        # was 80 findings out of 168.
                        if actual is None and want.lower() != "none":
                            continue
                        if is_prose(m.group('val')) or same_number(want, got):
                            continue
                        if want.strip("'\"") != got.strip("'\"") and want.lower() not in ('', 'none'):
                            res.append(dict(kind='B', repo=repo, file=str(path), line=fn.lineno,
                                            func=fn.name, param=dn, doc_default=m.group('val').strip(),
                                            real_default=got, type=typ.strip()[:70]))
    return res


# --------------------------------------------------------------------------


COUNTS: Dict[str, int] = {"files": 0, "funcs": 0, "props": 0, "compat_blind": 0}


def scan(root: str) -> List[dict]:
    """Walk the tree. Test files are skipped by name, not only by directory."""
    COUNTS.update(files=0, funcs=0, props=0, compat_blind=0)
    hits: List[dict] = []
    base = pathlib.Path(root)
    name = base.name
    for p in sorted(base.rglob("*.py")):
        parts = p.relative_to(base).parts
        if any(x in SKIP_DIRS or (x.startswith(".") and x not in common.KEEP_HIDDEN)
               for x in parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py":
            continue
        COUNTS["files"] += 1
        hits.extend(scan_file(p, name))
    return hits


def is_hard(hit: dict) -> bool:
    """Hard means class A only: a documented name absent from the signature."""
    return hit.get("kind") == "A"


def print_report(hits: List[dict], root: str, verbose: bool = False) -> None:
    hard = [h for h in hits if is_hard(h)]
    soft = [h for h in hits if not is_hard(h)]

    if hard:
        print(f"\n=== Documented name not in the signature ({len(hard)}) ===")
        for h in hard:
            print(f"\n  {h['file']}:{h['line']}  {h['func']}()")
            print(f"    docstring:  {h['param']}")
            print(f"    code:       {', '.join(h['sig'][:8])}")
    if soft:
        print(f"\n=== Documented default differs ({len(soft)}) ===")
        for h in soft[: (len(soft) if verbose else 30)]:
            print(f"\n  {h['file']}:{h['line']}  {h['func']}()  {h['param']}")
            print(f"    docstring:  {h['doc_default']}")
            print(f"    code:       {h['real_default']}")
        if not verbose and len(soft) > 30:
            print(f"\n  ... {len(soft) - 30} more, use -v for the full list")

    print("\n=== Coverage ===")
    print(f"  tree:                   {root}")
    print(f"  files read:             {COUNTS['files']}")
    print(f"  functions with Parameters: {COUNTS['funcs']}")
    print(f"  properties skipped:     {COUNTS['props']} (docstring describes what they return)")
    if COUNTS["compat_blind"]:
        print(f"  opaque decorator:       {COUNTS['compat_blind']} (cannot tell which extra names it accepts)")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="numpydoc docstrings against the signature")
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
