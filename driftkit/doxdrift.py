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
    hide the name inside parentheses, as an argument and as a declaration alike:
    `CvResult (CV_API_CALL *Capture_open)(const char* filename)` keeps its name
    in the first pair of parentheses and its arguments in the second, and
    reading the first pair as the argument list cost 111 false findings out of
    133 on opencv;
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

TWO ENGINES. The same findings can be reached two ways, and they are blind in
different places, which was measured rather than assumed:

  `--engine regex` (the default) reads the header as TEXT. It never asks whether
      the code compiles, so it runs on any tree, and it cannot tell which
      declaration a comment belongs to. That is what produced 25 false findings
      on asio before the family-of-overloads rule.

  `--engine clang` hands the file to the compiler and reads `-Wdocumentation`.
      It knows the real declaration and even suggests the name that was meant.
      Missing includes are fatal to the parse, so they get replaced with empty
      stub headers that accumulate per project; 2233 headers of Boost parsed
      that way with no build at all. Where a declaration still fails to parse,
      clang goes **silent** rather than wrong, so it under-reports exactly where
      the stubs were needed.

Both engines emit the same findings, so the refuter and the sweep cannot tell
them apart. Divergence between the two is material worth reading: it is the
cheapest way to catch a bug in either.

A practical use beyond precision: on a project that parses, the report can be
shown to a maintainer as the output of **their own compiler** rather than of
somebody's script.

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
import shutil
import subprocess
import sys
import tempfile
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


def _match_paren(decl, i):
    """Index of the parenthesis closing the one at `i`, or -1."""
    depth = 0
    for j in range(i, len(decl)):
        if decl[j] == '(':
            depth += 1
        elif decl[j] == ')':
            depth -= 1
            if depth == 0:
                return j
    return -1


def _find_call_paren(decl, start):
    """The first parenthesis that opens an argument list rather than a macro."""
    i = decl.find('(', start)
    while i >= 0:
        head = decl[start:i]
        if not MACRO_BEFORE_PAREN.search(head) and not KEYWORD_BEFORE_PAREN.search(head):
            return i
        # skip over the whole macro body
        j = _match_paren(decl, i)
        if j < 0:
            return -1
        start = j + 1
        i = decl.find('(', start)
    return -1


# A function pointer keeps its NAME in the first pair of parentheses and its
# arguments in the second: `CvResult (CV_API_CALL *Capture_open)(const char*
# filename, int camera_index, CvPluginCapture* handle)` and `typedef void
# (*MouseCallback)(int event, int x, int y, int flags, void* userdata)`. Read
# the first pair as the argument list and the function appears to take one
# argument named after itself, so every documented argument reads as missing.
# On opencv that was 111 false findings out of 133, in plugin tables and mouse
# callbacks. The species lives in every project with a C-compatible interface.
DECLARATOR = re.compile(
    r'^[^(),]*?[*&]\s*(?:[A-Za-z_]\w*\s*::\s*\*\s*)?[A-Za-z_]\w*\s*$')
OPENS_NEXT = re.compile(r'\s*\(')


def _is_declarator(inner, decl, close):
    """Do these parentheses hold a name rather than an argument list.

    Two things have to hold at once, and the second is what keeps an ordinary
    one-argument function safe: `void f(int *x)` looks exactly like a declarator
    on the inside, and is told apart by what follows the closing parenthesis.
    A declarator is always followed by the argument list itself.
    """
    return bool(DECLARATOR.match(inner)) and bool(OPENS_NEXT.match(decl[close + 1:]))


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
    while True:
        i = _find_call_paren(decl, start)
        if i < 0:
            return None
        j = _match_paren(decl, i)
        if j < 0:
            return None
        if not _is_declarator(decl[i+1:j], decl, j):
            break
        # the name of a function pointer, the arguments are in the next pair
        COUNTS["fnptr"] += 1
        start = j + 1
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
        # A macro carrying the default value sits after the parameter name:
        # `class Options BOOST_CONTAINER_DOCONLY(= void)`. Taking the last
        # identifier then yields the macro instead of `Options`, and every
        # `\tparam Options` in Boost.Container reads as undocumented. The same
        # guard already stood over function arguments; template parameters had
        # none.
        n = re.sub(r'\b[A-Z][A-Z0-9_]{3,}\s*\([^)]*\)\s*$', '', n).strip()
        n = n.split('=')[0].strip()
        m = re.findall(r'[A-Za-z_]\w*', n)
        if m: res.append(m[-1])
    return res

def line_of(text, pos):
    return text.count('\n', 0, pos) + 1


# --------------------------------------------------------------------------


COUNTS: Dict[str, int] = {"files": 0, "blocks": 0, "glued": 0, "skipped": 0,
                          "clang_parsed": 0, "clang_failed": 0, "stubs": 0,
                          "aliases": 0, "fnptr": 0}


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


# Header suffixes, not just the Boost one.
#
# For its first months this tool globbed '*.hpp' only. Boost writes .hpp, so
# it looked like it worked. Google, Chromium, LLVM and most of the C++ world
# write .h: the run over protobuf reported "headers read: 6" against 611 real
# headers, abseil 0 against 385, googletest 0 against 49. Zero findings read
# as "clean" when it meant "did not look". Blindness costs more than a false
# positive, because a false positive argues with you and blindness does not.
HEADER_GLOBS = ("*.hpp", "*.h", "*.hh", "*.hxx", "*.h++", "*.ipp", "*.inl")


def _headers(base: pathlib.Path):
    seen = set()
    for pattern in HEADER_GLOBS:
        for p in base.rglob(pattern):
            if p not in seen:
                seen.add(p)
                yield p


def scan(root: str) -> List[dict]:
    COUNTS.update(files=0, blocks=0, glued=0, skipped=0, fnptr=0)
    hits: List[dict] = []
    base = pathlib.Path(root)
    for p in sorted(_headers(base)):
        parts = p.relative_to(base).parts
        if any(x in SKIP_DIRS or (x.startswith(".") and x not in common.KEEP_HIDDEN)
               for x in parts):
            continue
        try:
            src = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            COUNTS["skipped"] += 1
            continue
        COUNTS["files"] += 1
        hits.extend(scan_text(src, str(p.relative_to(base))))
    return hits


# --------------------------------------------------------------------------
# Second engine: the compiler itself
# --------------------------------------------------------------------------

CLANG_WARN = re.compile(
    r"^(?P<file>[^\n:]+):(?P<line>\d+):\d+: warning: "
    r"(?:parameter|template parameter) '(?P<name>[^']+)' not found in the "
    r"(?P<where>function|template) declaration \[-Wdocumentation\]", re.M)
CLANG_MISSING = re.compile(r"'([^']+)' file not found")
CLANG_SUGGEST = re.compile(r"note: did you mean '([^']+)'\?")
STUB_ROUNDS = 25          # no Boost tree has needed more than this


def clang_available() -> bool:
    return shutil.which("clang++") is not None


def _include_dirs(root: str) -> List[str]:
    out = []
    for cand in ("include", "."):
        p = os.path.join(root, cand)
        if os.path.isdir(p):
            out.append(p)
    return out[:2]


def clang_file(path: str, root: str, stubs: str, counts: Dict[str, int]) -> List[dict]:
    """Findings in one header, taken from the compiler.

    Missing includes are replaced with empty stubs and the stubs accumulate for
    the whole project: the first header of a Boost library needs about seven,
    the rest almost none.
    """
    args = ["clang++", "-fsyntax-only", "-Wdocumentation", "-ferror-limit=0",
            "-std=c++17", "-I", stubs]
    for d in _include_dirs(root):
        args += ["-I", d]
    for _ in range(STUB_ROUNDS):
        try:
            p = subprocess.run(args + [path], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            counts["clang_failed"] += 1
            return []
        out = p.stderr
        m = CLANG_MISSING.search(out)
        if not m:
            counts["clang_parsed"] += 1
            return _clang_findings(out, path, root)
        target = os.path.join(stubs, m.group(1))
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            open(target, "w").close()
            counts["stubs"] += 1
        except OSError:
            counts["clang_failed"] += 1
            return []
    counts["clang_failed"] += 1
    return []


ALIAS_AT = re.compile(r"\\t?param_(?P<name>[A-Za-z_]\w*)")
_ALIASES_SEEN: set = set()


def is_project_alias(where: str, line: int, name: str) -> bool:
    """Is this a Doxygen alias defined by the project rather than a parameter.

    Boost.Geometry writes `\\param geometry \\param_geometry`, where the second
    word is an alias declared in its own Doxyfile. The compiler knows nothing
    about a Doxyfile, reads `\\param_geometry` as the command plus a name, and
    reports a parameter called `_geometry`. On that tree it produced 605
    warnings, and every one of them was this.

    An alias can carry an argument in braces, `\\param_strategy{Area}`, and the
    compiler then reports the whole thing as the name. Only the identifier part
    is compared, so both forms are recognised.

    The signal is exact: the command and the name written with no space between
    them. A real parameter is always `\\param name`.
    """
    if not name.startswith("_"):
        return False
    try:
        with open(where, encoding="utf-8", errors="replace") as fh:
            text = fh.read().splitlines()
    except OSError:
        return False
    if not 1 <= line <= len(text):
        return False
    stem = re.match(r"_?(\w*)", name)
    stem = stem.group(1) if stem else ""
    return any(m.group("name") == stem for m in ALIAS_AT.finditer(text[line - 1]))


def _clang_findings(out: str, path: str, root: str) -> List[dict]:
    hits: List[dict] = []
    lines = out.splitlines()
    for m in CLANG_WARN.finditer(out):
        # A warning can arrive from an included header, so the coordinate comes
        # from the warning itself rather than from the file being compiled.
        # Counting per compiled file counts mentions instead of entities: on
        # Boost.Geometry that gave 64,966 instead of 605.
        where = m.group("file")
        try:
            rel = os.path.relpath(where, root)
        except ValueError:
            rel = where
        suggest = ""
        tail = out[m.end(): m.end() + 400]
        sm = CLANG_SUGGEST.search(tail)
        if sm:
            suggest = sm.group(1)
        if is_project_alias(where, int(m.group("line")), m.group("name")):
            # Entities rather than mentions: the same warning arrives from
            # every file that includes the header.
            _ALIASES_SEEN.add((where, m.group("line"), m.group("name")))
            COUNTS["aliases"] = len(_ALIASES_SEEN)
            continue
        hits.append(dict(
            kind="param" if m.group("where") == "function" else "tparam",
            hard=True, file=rel, line=int(m.group("line")), name=m.group("name"),
            sig=[suggest] if suggest else [], decl="", engine="clang",
            note=f"clang suggests `{suggest}`" if suggest else "reported by clang -Wdocumentation",
        ))
    seen, uniq = set(), []
    for h in hits:
        key = (h["file"], h["line"], h["name"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq


def scan_clang(root: str) -> List[dict]:
    """The whole tree through the compiler. Stubs live in a temporary directory."""
    COUNTS.update(files=0, blocks=0, glued=0, skipped=0,
                  clang_parsed=0, clang_failed=0, stubs=0, aliases=0)
    _ALIASES_SEEN.clear()
    hits: List[dict] = []
    base = pathlib.Path(root)
    stubs = tempfile.mkdtemp(prefix="doxdrift-stubs-")
    seen = set()
    try:
        for p in sorted(_headers(base)):
            parts = p.relative_to(base).parts
            if any(x in SKIP_DIRS or (x.startswith(".") and x not in common.KEEP_HIDDEN)
                   for x in parts):
                continue
            COUNTS["files"] += 1
            for h in clang_file(str(p), root, stubs, COUNTS):
                key = (h["file"], h["line"], h["name"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append(h)
    finally:
        shutil.rmtree(stubs, ignore_errors=True)
    return hits


def print_report(hits: List[dict], root: str, verbose: bool = False,
                 engine: str = "regex") -> None:
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
    print(f"  engine:                 {engine}")
    print(f"  headers read:           {COUNTS['files']}")
    print(f"  headers skipped:        {COUNTS['skipped']} (unreadable)")
    print(f"  blocks with \\param:     {COUNTS['blocks']}")
    if engine == "clang":
        print(f"  headers the compiler parsed: {COUNTS['clang_parsed']}")
        print(f"  headers it could not:   {COUNTS['clang_failed']}")
        print(f"  stub headers created:   {COUNTS['stubs']} (empty, for missing includes)")
        print(f"  project aliases skipped:{COUNTS['aliases']} (\\param_name from the project Doxyfile)")
    else:
        print(f"  family blocks:          {COUNTS['glued']} (a name repeats, cannot judge)")
        print(f"  function pointers read: {COUNTS['fnptr']} (the name is in parentheses, the arguments follow)")
    print(common.findings_line(len(hits), 0))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Doxygen \\param against the C++ signature")
    ap.add_argument("root", help="directory holding the headers")
    ap.add_argument("--engine", choices=("regex", "clang"), default="regex",
                    help="regex reads the text, clang asks the compiler")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    if args.engine == "clang" and not clang_available():
        sys.exit("clang++ not found: install it or use --engine regex")
    hits = scan_clang(args.root) if args.engine == "clang" else scan(args.root)
    print_report(hits, args.root, args.verbose, args.engine)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([dict(h, hard=True) for h in hits], fh, ensure_ascii=False, indent=1)
    return 1 if any(h.get("hard") for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
