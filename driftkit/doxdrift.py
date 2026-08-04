#!/usr/bin/env python3
"""doxdrift.py: Doxygen `\\param` against the actual C++ signature.

The documentation species in a language where hardly anyone checks it. Doxygen
complains about this only with `WARN_IF_DOC_ERROR` on, and in a large tree its
output drowns, so the mismatches live for years. This check produced merges in
Boost.

TWO CLASSES OF FINDING, both hard:

  - **a `\\param NAME` that is not in the argument list.** Usually the argument
    was renamed and the comment line stayed;
  - **a `\\tparam NAME` that is not among the template parameters.** Same thing
    for templates.

WHY THIS IS HARDER THAN PYTHON. There the signature comes from `ast`; here it
comes from regular expressions, because parsing C++ properly needs a
preprocessor and a compiler. Hence a list of caveats, every one of them paid
for with false findings on Boost:

  - parentheses of a macro in the return type (`BOOST_BEAST_ASYNC_RESULT2(Handler)`)
    were taken for an argument list;
  - `operator()` and `operator[]` carry parentheses inside the name itself;
  - `decltype(auto)`, `noexcept(...)` and `sizeof(...)` are no argument lists;
  - an array by reference `char(&dest)[N]` and a function pointer `void(*cb)(int)`
    hide the name inside parentheses;
  - preprocessor directives together with their `\\` continuations are stripped
    before the parse.

KNOWN BLIND SPOTS:
  - **`.hpp` headers only.** Files `.h`, `.cpp` and `.cc` are not read;
  - declarations longer than 900 characters are cut off. Looking further makes
    no sense, and no finding will come from there either;
  - macros that assemble a whole signature are skipped on purpose (`#define`,
    `BOOST_GEOMETRY_`): there is nothing to parse them with;
  - directories `test`, `example`, `doc` and `extensions` are not read;
  - overloads: when several declarations sit together, the comment is compared
    against the nearest one rather than all of them.

Run:
  python3 doxdrift.py ~/Projects/oss/boost/libs/gil
  python3 doxdrift.py ~/Projects/oss/boost/libs/beast --json out.json

Tests: test_doxdrift.py next to this file.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import pathlib
import re
import sys
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

# The shared skip list plus directories where comments are written to illustrate.
SKIP_DIRS = common.SKIP_DIRS | {"test", "tests", "example", "examples", "doc", "docs",
                               "extensions"}

BLOCK = re.compile(r'/\*[!*](.*?)\*/\s*(.{0,900}?)(?:\{|;)', re.S)
SLASH = re.compile(r'((?:^[ \t]*///[^\n]*\n)+)([\s\S]{0,900}?)(?:\{|;)', re.M)
PARAM = re.compile(r'[\\@]param\s*(?:\[[^\]]*\])?\s+([A-Za-z_]\w*)')
TPARAM = re.compile(r'[\\@]tparam\s+([A-Za-z_]\w*)')


def _skip_template(decl):
    """Position right after the template part, when there is one."""
    m = re.match(r'\s*template\s*<', decl)
    if not m:
        return 0
    i = decl.index('<'); depth = 0
    for j in range(i, len(decl)):
        if decl[j] == '<': depth += 1
        elif decl[j] == '>':
            depth -= 1
            if depth == 0:
                return j + 1
    return 0


def _strip_preprocessor(decl):
    """Strip preprocessor directives together with their `\\` continuations."""
    out, skipping = [], False
    for l in decl.split('\n'):
        if skipping or l.lstrip().startswith('#'):
            # the directive continues while the line ends with a backslash
            skipping = l.rstrip().endswith('\\')
            continue
        out.append(l)
    return '\n'.join(out)


# parentheses belonging to something other than an argument list: an uppercase
# macro and language constructs (decltype(auto), noexcept(...), sizeof(...))
MACRO_BEFORE_PAREN = re.compile(r'([A-Z][A-Z0-9_]{3,})\s*$')
KEYWORD_BEFORE_PAREN = re.compile(
    r'\b(decltype|noexcept|sizeof|alignas|alignof|static_assert|__attribute__|'
    r'__declspec|deprecated|defined|explicit|constexpr|requires)\s*$')


def _find_call_paren(decl, start):
    """The first parenthesis that opens an argument list rather than a macro."""
    i = decl.find('(', start)
    while i >= 0:
        head = decl[start:i]
        if not MACRO_BEFORE_PAREN.search(head) and not KEYWORD_BEFORE_PAREN.search(head):
            return i
        # skip over the whole macro body
        depth, j = 0, i
        while j < len(decl):
            if decl[j] == '(':
                depth += 1
            elif decl[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        else:
            return -1
        start = j + 1
        i = decl.find('(', start)
    return -1


def sig_params(decl):
    """Argument identifiers taken from the parentheses of a declaration."""
    # operator() and operator[] carry parentheses inside the name: without this
    # find('(') lands in the empty parentheses of the name and the list is lost
    decl = re.sub(r'operator\s*\(\s*\)', 'operator_call', decl)
    decl = re.sub(r'operator\s*\[\s*\]', 'operator_index', decl)
    start = _skip_template(decl)
    # a macro in the return type carries its own parentheses:
    # BOOST_BEAST_ASYNC_RESULT2(Handler); without skipping them they are taken
    # for the argument list of the function
    i = _find_call_paren(decl, start)
    if i < 0:
        return None
    depth, j = 0, i
    while j < len(decl):
        if decl[j] == '(': depth += 1
        elif decl[j] == ')':
            depth -= 1
            if depth == 0: break
        j += 1
    else:
        return None
    inner = decl[i+1:j]
    names, depth, cur = [], 0, ''
    for ch in inner:
        if ch in '<([{': depth += 1
        elif ch in '>)]}': depth -= 1
        if ch == ',' and depth == 0:
            names.append(cur); cur = ''
        else:
            cur += ch
    names.append(cur)
    out = []
    for n in names:
        n = n.split('=')[0].strip()
        # array by reference and function pointer: char(&dest)[N], void(*cb)(int)
        m2 = re.search(r'\(\s*[&*]\s*([A-Za-z_]\w*)\s*\)', n)
        if m2:
            out.append(m2.group(1)); continue
        # trailing dimensions and bracketed suffixes are dropped
        n = re.sub(r'\[[^\]]*\]', '', n)
        # a macro tail after the name: ReadToken&& token BOOST_ASIO_DEFAULT_TOKEN(Ex).
        # otherwise the macro argument gets taken for the parameter name.
        # when the whole argument IS a macro (BOOST_URL_STRTOK_ARG(token)) the
        # name lives inside it, so the tail stays
        trimmed = re.sub(r'\b[A-Z][A-Z0-9_]{3,}\s*\([^)]*\)\s*$', '', n).strip()
        if trimmed and re.search(r'[A-Za-z_]\w*', trimmed):
            n = trimmed
        # an unnamed constraint argument: constraint_t<...> with no name
        if n.endswith('>') or not n:
            continue
        m = re.findall(r'[A-Za-z_]\w*', n)
        if m: out.append(m[-1])
    return out

def tpl_params(decl):
    m = re.match(r'\s*template\s*<', decl)
    if not m: return []
    i = decl.index('<'); depth, j = 0, i
    while j < len(decl):
        if decl[j] == '<': depth += 1
        elif decl[j] == '>':
            depth -= 1
            if depth == 0: break
        j += 1
    inner = decl[i+1:j]
    out, depth, cur = [], 0, ''
    for ch in inner:
        if ch in '<([{': depth += 1
        elif ch in '>)]}': depth -= 1
        if ch == ',' and depth == 0:
            out.append(cur); cur = ''
        else: cur += ch
    out.append(cur)
    res = []
    for n in out:
        n = n.split('=')[0].strip()
        m = re.findall(r'[A-Za-z_]\w*', n)
        if m: res.append(m[-1])
    return res

def line_of(text, pos):
    return text.count('\n', 0, pos) + 1


# --------------------------------------------------------------------------


COUNTS: Dict[str, int] = {"files": 0, "blocks": 0, "glued": 0}


def scan_text(src: str, rel: str) -> List[dict]:
    """Findings in one header. A separate function so tests need no disk."""
    hits: List[dict] = []
    for m in itertools.chain(BLOCK.finditer(src), SLASH.finditer(src)):
        doc, decl = m.group(1), m.group(2)
        decl = _strip_preprocessor(decl)
        pnames = PARAM.findall(doc)
        tnames = TPARAM.findall(doc)
        if not pnames and not tnames:
            continue
        COUNTS["blocks"] += 1
        if '#define' in decl or 'BOOST_GEOMETRY_' in decl.split('(')[0]:
            continue
        # The same `\param` name twice in one block means the block does NOT
        # describe a single function. asio documents families of overloads this
        # way: the comment lists `ex`, `token`, `context`, `token`,
        # `peer_endpoint` while one declaration with one argument sits next to
        # it. Twenty-five false findings on asio, all of this nature.
        if len(pnames) != len(set(pnames)) or len(tnames) != len(set(tnames)):
            COUNTS["glued"] += 1
            continue
        sp = sig_params(decl)
        tp = tpl_params(decl)
        ln = line_of(src, m.start())
        if sp is not None:
            for n in pnames:
                if n not in sp:
                    hits.append(dict(kind='param', hard=True, file=rel, line=ln, name=n,
                                     sig=sp, decl=' '.join(decl.split())[:120]))
        if tp:
            for n in tnames:
                if n not in tp:
                    hits.append(dict(kind='tparam', hard=True, file=rel, line=ln, name=n,
                                     sig=tp, decl=' '.join(decl.split())[:120]))
    return hits


def scan(root: str) -> List[dict]:
    COUNTS.update(files=0, blocks=0, glued=0)
    hits: List[dict] = []
    base = pathlib.Path(root)
    for p in sorted(base.rglob('*.hpp')):
        parts = p.relative_to(base).parts
        if any(x in SKIP_DIRS or (x.startswith(".") and x not in common.KEEP_HIDDEN)
               for x in parts):
            continue
        try:
            src = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        COUNTS["files"] += 1
        hits.extend(scan_text(src, str(p.relative_to(base))))
    return hits


def print_report(hits: List[dict], root: str, verbose: bool = False) -> None:
    par = [h for h in hits if h["kind"] == "param"]
    tpar = [h for h in hits if h["kind"] == "tparam"]
    for title, items in (("Documented \\param missing from the signature", par),
                         ("Documented \\tparam missing from the template", tpar)):
        if not items:
            continue
        print(f"\n=== {title} ({len(items)}) ===")
        for h in items[: (len(items) if verbose else 40)]:
            print(f"\n  {h['file']}:{h['line']}")
            print(f"    in the comment:     {h['name']}")
            print(f"    in the declaration: {', '.join(h['sig'][:8]) or '(empty)'}")
            if verbose:
                print(f"    declaration:        {h['decl']}")
        if not verbose and len(items) > 40:
            print(f"\n  ... {len(items) - 40} more, use -v for all")

    print("\n=== Coverage ===")
    print(f"  tree:                   {root}")
    print(f"  headers read:           {COUNTS['files']}")
    print(f"  blocks with \\param:     {COUNTS['blocks']}")
    print(f"  family blocks:          {COUNTS['glued']} (a name repeats, cannot judge)")
    print(common.findings_line(len(hits), 0))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Doxygen \\param against the C++ signature")
    ap.add_argument("root", help="directory holding the headers")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    hits = scan(args.root)
    print_report(hits, args.root, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([dict(h, hard=True) for h in hits], fh, ensure_ascii=False, indent=1)
    return 1 if any(h.get("hard") for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
