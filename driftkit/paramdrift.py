#!/usr/bin/env python3
"""paramdrift.py: a documented parameter name against the signature under it,
in the languages whose markup is a tag.

WHY ONE MODULE AND NOT SIX. The species is already known: `docdrift` finds it
in Python, `doxdrift` in C and C++, `swiftdrift` in Swift. What was not obvious
until this week is how much of the remaining work is shared:

    /** @param count how many */        JSDoc, JavaScript and TypeScript
     * @param count how many            Javadoc, Java
    /** @param int $count how many */   PHPDoc, PHP
    # @param [Integer] count            YARD, Ruby
    /// <param name="count">…</param>   XML doc, C#

Four of those five are the same three characters, `@param`, followed by a name.
The markup does not differ. **The signature underneath it does.** Six separate
tools would have meant six copies of the flags, the report, the JSON contract
and the tests, to hold six copies of one comparison. So the languages live in a
registry here and each supplies two functions: read the names out of a comment,
read the names out of a declaration.

WHAT IS REPORTED, hard:

  - **a documented name the declaration does not have.** The argument was
    renamed, or removed, and the line above it stayed.

DELIBERATELY NOT REPORTED:

  - **an undocumented parameter.** Incompleteness, not drift, and in these
    languages it is the normal state of most code;
  - anything where the declaration could not be read with certainty. A
    signature that is not understood produces no finding, and the report says
    how many there were. See the next paragraph, it is the important one.

🔴 THE COUNTER THAT HAD TO EXIST BEFORE THE FIRST LANGUAGE. A report reading
`functions with Parameters: 14` on a tree of 1232 functions looks healthy, and
that is exactly what a broken parser prints. It happened on keras, and the
usual guard, "zero findings on a non-empty tree", does not catch it: fourteen
is not zero. So every run here prints **how many documented comments were bound
to a declaration out of how many were seen**, per language. A number that falls
means the parser went blind, whatever the findings say.

WHAT MAKES EACH LANGUAGE AWKWARD, since the traps are not shared even though
the markup is:

  - **JSDoc puts the type before the name**, `@param {string} name`, the
    opposite way round from Doxygen, and the braces nest: `{Array<{x: number}>}`
    cannot be matched with one regular expression;
  - **a JSDoc name can be optional or defaulted**, `[name]` and `[name=1]`, and
    **a name can be a path**, `opts.retries`. The path documents a field of an
    argument, not an argument, so only the root is compared. Comparing the whole
    path would manufacture a finding on every well-documented options object;
  - **a destructured parameter has no name at all**, `function f({a, b})`. There
    is nothing to compare against, so such a declaration is skipped and counted
    rather than guessed at;
  - `@typedef` and `@callback` blocks carry `@param` lines describing **a type,
    not the function below them**. Binding those to the next declaration would
    be a false finding every time.

KNOWN BLIND SPOTS, named rather than hidden:

  - **minified files.** A committed `.min.js` is not in any skip list and has
    no comments worth reading; a file whose longest line runs past 5000
    characters is skipped and counted;
  - **overloads are unioned rather than told apart.** One comment above an
    interface holding two call signatures documents both, so the names of both
    are accepted. A name that belongs to the second overload and is written as
    if it belonged to the first goes unreported. That is the deliberate
    direction: the first run of this check against hono reported exactly such
    a name, and it was not a defect;
  - **generated code** is invisible here as it is to every other check in the
    kit;
  - a doc comment separated from its declaration by a statement is left
    unbound on purpose: a file header followed by imports must not bind to the
    first function in the file.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import sys
from dataclasses import dataclass, field as dc_field
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
import stamp  # noqa: E402

TOOL = "paramdrift"

# How far below a doc comment a declaration may start. Long enough for a
# decorated method, short enough that a file header does not reach the first
# function in the file.
WINDOW = 900

# A line longer than this means the file is minified. Nothing in it is worth
# reading and the parenthesis matcher would walk the whole line.
MAX_LINE = 5000

# A parameter that cannot be named: `function f({a, b})`. Not a failure to
# parse, a declaration with nothing to compare against.
OPAQUE = "\0opaque"


# --------------------------------------------------------------------------
# Shared pieces
# --------------------------------------------------------------------------

@dataclass
class Block:
    """A documentation comment and where it sits."""
    text: str      # the comment body, decoration blanked, offsets preserved
    line: int      # 1-based line of the opening of the comment
    body: int      # character offset in the source at which `text` begins
    end: int       # character offset just past the closing of the comment


def line_index(src: str) -> List[int]:
    """Offsets at which each line starts, for turning a position into a line."""
    starts = [0]
    for m in re.finditer("\n", src):
        starts.append(m.end())
    return starts


def line_at(starts: List[int], pos: int) -> int:
    return bisect.bisect_right(starts, pos)


def skip_blank(text: str, i: int) -> int:
    """Past spaces and tabs. Newlines are not crossed on purpose: a tag with
    nothing after it on its line must not swallow the next line."""
    while i < len(text) and text[i] in " \t":
        i += 1
    return i


def match_pair(text: str, i: int, opener: str = "(", closer: str = ")") -> int:
    """Index of the bracket closing the one at `i`, or -1."""
    if i >= len(text) or text[i] != opener:
        return -1
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return j
    return -1


# --------------------------------------------------------------------------
# JavaScript and TypeScript
# --------------------------------------------------------------------------

# `/**` opens a doc comment; `/**/` is an empty ordinary one and is not.
JS_DOC = re.compile(r"/\*\*(?!/)(.*?)\*/", re.S)

# The decoration down the left margin. Replaced by a space rather than
# removed, so every offset inside the block stays where it was and a finding
# can be reported on the line it is actually on.
JS_MARGIN = re.compile(r"^([ \t]*)\*([ \t]?)", re.M)

# A block describing a type rather than the declaration below it.
JS_NOT_A_FUNCTION = re.compile(r"@(?:typedef|callback|external|enum)\b")

JS_TAG = re.compile(r"(?<![\w$@])@(?:param|arg|argument)\b")

IDENT = re.compile(r"[A-Za-z_$][\w$]*")

# Keywords that take a parenthesis and are not declarations.
JS_STOP = frozenset({
    "if", "for", "while", "switch", "catch", "return", "typeof", "await",
    "new", "delete", "void", "in", "of", "do", "else", "case", "yield",
    "instanceof", "import", "require", "throw", "with", "super",
})

JS_MODIFIER = re.compile(
    r"^(?:public|private|protected|readonly|override|abstract|static)\s+")
JS_DECORATOR = re.compile(r"^@[\w$.]+(?:\([^)]*\))?\s*")


def js_blocks(src: str) -> Iterator[Block]:
    starts = line_index(src)
    for m in JS_DOC.finditer(src):
        body = JS_MARGIN.sub(r"\1 \2", m.group(1))
        yield Block(body, line_at(starts, m.start()), m.start(1), m.end())


def js_doc_params(text: str) -> Optional[List[Tuple[str, int]]]:
    """Documented names and their offsets inside the block.

    `None` means the block describes a type and must not be bound to anything.
    """
    if JS_NOT_A_FUNCTION.search(text):
        return None
    out: List[Tuple[str, int]] = []
    seen = set()
    for m in JS_TAG.finditer(text):
        i = skip_blank(text, m.end())
        if i < len(text) and text[i] == "{":
            j = match_pair(text, i, "{", "}")
            if j < 0:
                continue
            i = skip_blank(text, j + 1)
        name = _js_doc_name(text, i)
        if name and name not in seen:
            seen.add(name)
            out.append((name, m.start()))
    return out


def _js_doc_name(text: str, i: int) -> Optional[str]:
    if i >= len(text) or text[i] == "\n":
        return None
    if text[i] == "[":
        j = match_pair(text, i, "[", "]")
        if j < 0:
            return None
        raw = text[i + 1:j].split("=", 1)[0]
    else:
        m = re.match(r"\S+", text[i:])
        if not m:
            return None
        raw = m.group(0).rstrip(",;:")
    raw = raw.strip()
    if raw.startswith("..."):
        raw = raw[3:]
    # `opts.retries` documents a field of an argument, not an argument.
    raw = raw.split(".", 1)[0].strip()
    return raw if re.fullmatch(r"[A-Za-z_$][\w$]*", raw) else None


def js_blank(src: str) -> str:
    """Blank out strings and comments, keeping every offset and newline.

    A template literal is blanked whole, `${…}` included. Code inside one is
    never a declaration we are looking for, and keeping the braces would only
    confuse the bracket matcher.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and src[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            if i < n:
                out[i] = " "
                out[min(i + 1, n - 1)] = " "
                i += 2
        elif c in "'\"`":
            quote = c
            out[i] = " "
            i += 1
            while i < n:
                if src[i] == "\\":
                    if src[i] != "\n":
                        out[i] = " "
                    if i + 1 < n and src[i + 1] != "\n":
                        out[i + 1] = " "
                    i += 2
                    continue
                if src[i] == quote:
                    out[i] = " "
                    i += 1
                    break
                if src[i] != "\n":
                    out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def _prev_token(text: str, i: int) -> str:
    """The word or symbol immediately before position `i`."""
    j = i - 1
    while j >= 0 and text[j] in " \t\n\r":
        j -= 1
    if j < 0:
        return ""
    if text[j].isalnum() or text[j] in "_$":
        k = j
        while k >= 0 and (text[k].isalnum() or text[k] in "_$"):
            k -= 1
        return text[k + 1:j + 1]
    return text[j]


def js_signature(window: str) -> Optional[Tuple[str, List[str]]]:
    """The declared name and its parameter names, or `None` if not bound.

    🔴 OVERLOADS ARE UNIONED, and the first real finding this check produced
    was the reason. Hono documents `HTMLRespond` once above an interface that
    holds two call signatures; the first takes three arguments, the second
    takes `init`. Binding to the first alone reports `init` as drift, and it
    is not drift, it is the second signature. So a declaration in **type
    position** (its parameter list is followed by `:` or `;` rather than a
    body) continues to be read: every call signature at the same brace depth
    contributes its names, and the set closes when the depth drops.

    A declaration with a body ends the reading immediately. Otherwise a
    function would take the names of every function nested inside it.
    """
    cut = window.find("/**")
    if cut >= 0:
        window = window[:cut]
    blank = js_blank(window)
    bound = False
    symbol, names = "", []
    depth, first_depth, angle, prev_char = 0, 0, 0, ""
    i, n = 0, len(blank)
    while i < n:
        c = blank[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            # A statement that closes before any signature means the comment
            # and the code below it are not related.
            if not bound:
                return None
            if depth < first_depth:
                break
        elif c == ";":
            if not bound:
                return None
        elif c == "<":
            angle += 1
        elif c == ">":
            # `=>` is an arrow, not a closing angle bracket.
            if prev_char != "=":
                angle = max(0, angle - 1)
        elif c == "(" and angle == 0:
            prev = _prev_token(blank, i)
            close = match_pair(blank, i)
            if prev in JS_STOP or close < 0:
                if not bound:
                    return None
                break
            after = blank[close + 1:].lstrip()
            is_body = after[:2] == "=>" or after[:1] == "{"
            is_type = after[:1] in (":", ";")
            if not (is_body or is_type):
                if not bound:
                    return None
                i, prev_char = close + 1, ")"
                continue
            got = _js_params(blank[i + 1:close])
            if not bound:
                bound, first_depth = True, depth
                symbol, names = _js_symbol(blank[:i]), list(got)
                if is_body:
                    return symbol, names
            elif depth == first_depth:
                names.extend(g for g in got if g not in names)
            i, prev_char = close + 1, ")"
            continue
        prev_char = c
        i += 1
    return (symbol, names) if bound else None


# Words that may sit immediately before a parameter list without being the
# name of the thing being declared.
JS_TAIL_WORDS = re.compile(
    r"(?:\b(?:function|async|export|default|static|new|await|return|get|set)"
    r"\b\s*\*?\s*)+$")


def _js_symbol(head: str) -> str:
    """The name of the thing being declared, for the report. Cosmetic: a wrong
    answer here mislabels a finding, it does not create one."""
    head = head.rstrip()
    if head.endswith(">"):
        depth, j = 0, len(head) - 1
        while j >= 0:
            if head[j] == ">" and (j == 0 or head[j - 1] != "="):
                depth += 1
            elif head[j] == "<":
                depth -= 1
                if depth == 0:
                    break
            j -= 1
        head = head[:max(j, 0)].rstrip()
    head = JS_TAIL_WORDS.sub("", head).rstrip()
    m = re.search(r"([A-Za-z_$][\w$]*)$", head)
    if m:
        return m.group(1)
    m = re.search(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b[^=]*=\s*$", head)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z_$][\w$]*)\s*[:=]\s*$", head)
    return m.group(1) if m else ""


def _js_params(inner: str) -> List[str]:
    out = []
    for part in _split_top(inner):
        name = _js_param_name(part)
        if name is not None:
            out.append(name)
    return out


def _split_top(inner: str) -> List[str]:
    """Split on commas that are not inside brackets.

    Angle brackets count, because `a: Map<string, number>` holds a comma that
    is not a separator. `=>` is not a closing angle bracket: a default value
    may be an arrow function.
    """
    parts, cur, depth, angle = [], [], 0, 0
    prev = ""
    for c in inner:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "<":
            angle += 1
        elif c == ">" and prev != "=":
            angle = max(0, angle - 1)
        if c == "," and depth == 0 and angle == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        prev = c
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _js_param_name(part: str) -> Optional[str]:
    p = part.strip()
    while True:
        stripped = JS_DECORATOR.sub("", p)
        stripped = JS_MODIFIER.sub("", stripped)
        if stripped == p:
            break
        p = stripped.lstrip()
    if p.startswith("..."):
        p = p[3:].lstrip()
    if not p:
        return None
    if p[0] in "{[":
        return OPAQUE
    m = IDENT.match(p)
    if not m:
        return OPAQUE
    name = m.group(0)
    # TypeScript lets a method declare the type of `this`. It is not an
    # argument and nobody documents it.
    if name == "this":
        return None
    return name


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

@dataclass
class Lang:
    name: str
    exts: Tuple[str, ...]
    blocks: Callable[[str], Iterator[Block]]
    doc_params: Callable[[str], Optional[List[Tuple[str, int]]]]
    signature: Callable[[str], Optional[Tuple[str, List[str]]]]


LANGS: Dict[str, Lang] = {
    "js": Lang(
        name="js",
        exts=(".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
        blocks=js_blocks,
        doc_params=js_doc_params,
        signature=js_signature,
    ),
}

EXT_TO_LANG = {ext: lang for lang in LANGS.values() for ext in lang.exts}


# --------------------------------------------------------------------------
# The scan
# --------------------------------------------------------------------------

@dataclass
class Report:
    files: int = 0
    files_skipped: int = 0
    blocks: int = 0
    blocks_typedef: int = 0
    blocks_with_params: int = 0
    bound: int = 0
    opaque: int = 0
    findings: List[dict] = dc_field(default_factory=list)
    by_lang: Dict[str, List[int]] = dc_field(default_factory=dict)


def scan_text(src: str, rel: str, lang: Lang, report: Report) -> None:
    starts = line_index(src)
    seen = report.by_lang.setdefault(lang.name, [0, 0])
    for block in lang.blocks(src):
        report.blocks += 1
        documented = lang.doc_params(block.text)
        if documented is None:
            report.blocks_typedef += 1
            continue
        if not documented:
            continue
        report.blocks_with_params += 1
        seen[1] += 1
        sig = lang.signature(src[block.end:block.end + WINDOW])
        if sig is None:
            continue
        report.bound += 1
        seen[0] += 1
        symbol, params = sig
        if OPAQUE in params:
            report.opaque += 1
            continue
        have = set(params)
        for name, pos in documented:
            if name in have:
                continue
            report.findings.append({
                "tool": TOOL,
                "lang": lang.name,
                "where": rel,
                "line": line_at(starts, block.body + pos),
                "symbol": symbol,
                "documented": name,
                "signature": params,
                "hard": True,
                "why": (f"the comment names `{name}`, the declaration takes "
                        + (", ".join(f"`{p}`" for p in params) or "nothing")),
            })


def scan(root: str, langs: Sequence[str]) -> Report:
    report = Report()
    wanted = {ext for name in langs for ext in LANGS[name].exts}
    for dirpath, _dirs, names in common.walk(root):
        for fn in names:
            ext = os.path.splitext(fn)[1]
            if ext not in wanted:
                continue
            if fn.endswith((".min.js", ".min.ts", ".bundle.js")):
                report.files_skipped += 1
                continue
            path = os.path.join(dirpath, fn)
            src = common.read_text(path)
            if not src:
                report.files_skipped += 1
                continue
            if max((len(ln) for ln in src.splitlines()), default=0) > MAX_LINE:
                report.files_skipped += 1
                continue
            report.files += 1
            scan_text(src, os.path.relpath(path, root), EXT_TO_LANG[ext], report)
    return report


# --------------------------------------------------------------------------
# The report
# --------------------------------------------------------------------------

def print_report(report: Report, root: str, verbose: bool = False) -> None:
    for f in report.findings:
        where = f"{f['where']}:{f['line']}"
        symbol = f"{f['symbol']}: " if f["symbol"] else ""
        print(f"{where}  {symbol}{f['why']}")
    if not report.findings:
        print("no documented name is missing from its declaration")
    print()
    print("=== Coverage ===")
    print(f"  tree:                   {root}")
    print(f"  files read:             {report.files}")
    print(f"  files skipped:          {report.files_skipped} "
          f"(empty, huge or minified)")
    print(f"  doc comments:           {report.blocks}")
    print(f"  of those naming args:   {report.blocks_with_params}")
    print(f"  describing a type:      {report.blocks_typedef} "
          f"(@typedef and @callback, not bound to anything)")
    for name, (bound, total) in sorted(report.by_lang.items()):
        pct = (100.0 * bound / total) if total else 0.0
        # 🔴 The line that catches a parser going blind. A report showing
        # fourteen parsed comments out of twelve hundred looks healthy until
        # this percentage is printed next to it.
        print(f"  {name}: bound to a decl:   {bound} of {total} "
              f"documented ({pct:.0f}% parsed)")
    print(f"  nothing to compare:     {report.opaque} "
          f"(a destructured argument has no name)")
    print(common.findings_line(len(report.findings), 0))
    print(stamp.line(__file__, ("common.py",)))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="documented parameter names against the signature under "
                    "them, in the languages whose markup is a tag")
    ap.add_argument("root", help="the tree to read")
    ap.add_argument("--lang", action="append", choices=sorted(LANGS),
                    help="restrict to one language; repeatable")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    report = scan(args.root, args.lang or sorted(LANGS))
    print_report(report, args.root, args.verbose)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([dict(f, hard=bool(f["hard"]))
                       for f in report.findings], fh, indent=2)
    return 1 if report.findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
