#!/usr/bin/env python3
"""swiftdrift.py: a Swift doc comment `- Parameter` against the real signature.

The same species as docdrift and doxdrift, in the one language where nobody
checks it at all. Swift has no `-Wdocumentation` and no numpydoc: DocC builds
the documentation site and stays silent about a parameter it cannot bind, so a
renamed argument leaves its old name in the comment and nothing ever says so.

WHAT SWIFT MAKES HARDER THAN THE OTHER TWO. In Python a parameter has one
name and `ast` hands it over. In C++ it has one name and a regular expression
can find it. In Swift **an argument has two names**:

    func move(from start: Point, to end: Point)
                ^^^^ ^^^^^          ^^ ^^^
                label name          label name

    func greet(_ person: String)     // label suppressed, name is `person`
    func greet(person: String)       // label and name are both `person`

Apple's own convention documents the **parameter name**, the second one, and
so does the standard library. But plenty of code documents the label instead,
and both read naturally to a human. Guessing which one a project meant would
manufacture findings, so:

  **a documented name is accepted when it matches EITHER the label OR the
  parameter name of ANY argument. Only a name matching neither is a finding.**

That rule is the whole reason this check can be pointed at somebody else's
tree without drowning them in noise.

THE ONE CLASS OF FINDING, hard:

  - **a documented name that is neither a label nor a parameter name.** The
    argument was renamed, or removed, and the doc line stayed.

Both spellings of the markup are read:

    /// - Parameter person: the person to greet
    /// - Parameters:
    ///   - person: the person to greet
    ///   - loudly: whether to shout

DELIBERATELY NOT REPORTED:

  - **an undocumented parameter.** That is incompleteness, not drift, and in
    Swift it is the normal state of most code. Reporting it would bury the
    real finding under thousands of lines;
  - `- Returns:` on a function returning nothing, and other markup that does
    not name an argument;
  - a declaration with no doc comment at all.

WHY THE FILE IS SCANNED WITH A LEXER RATHER THAN REGULAR EXPRESSIONS. Three
things in Swift break the naive approach, and each of them puts a `func` where
there is none:

  - **block comments nest.** `/* outer /* inner */ still a comment */` is legal
    Swift and illegal C. A stripper written for C ends the comment early and
    reads prose as code;
  - **string interpolation contains code.** `"\(items.map { f(x: 1) })"` holds
    parentheses, braces and a colon inside a literal;
  - **raw strings** `#"..."#` and `##"..."##` turn off escaping, and multiline
    strings `\"\"\"` run across lines. A quote counter loses its place on the
    first one.

So the source is walked once, character by character, and everything that is
not code is blanked out while keeping every newline in place. Line numbers of
findings stay exact, and the parser after it sees only code.

KNOWN BLIND SPOTS, named rather than hidden:

  - **overloads.** A doc comment is compared against the declaration that
    follows it. Where several overloads sit together, each comment is matched
    to its own neighbour, which is right in the common case and wrong when a
    single comment covers a family;
  - **protocol requirements** and their implementations carry separate doc
    comments; both are checked, neither is compared against the other;
  - **`#if` branches.** A declaration inside a conditional block is read as
    ordinary code, so a comment before `#if` binds to the first branch;
  - **macros and property wrappers** that generate declarations are invisible,
    the same way generated code is invisible to every other check here;
  - a parameter list longer than 4000 characters is skipped rather than
    guessed at.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

TOOL = "swiftdrift"
MAX_PARAM_LIST = 4000


# --------------------------------------------------------------------------
# Lexing: blank out everything that is not code, keep the offsets
# --------------------------------------------------------------------------

@dataclass
class DocBlock:
    """A doc comment and where it sits."""
    text: str
    line: int          # 1-based line of the first line of the comment
    end: int           # character offset just past the comment


def scan(src: str) -> Tuple[str, List[DocBlock]]:
    """Return the source with non-code blanked, and the doc comments found.

    Blanking rather than deleting keeps every offset and every newline where
    it was, so a finding can be reported at the line it is actually on.
    """
    out = list(src)
    docs: List[DocBlock] = []
    i, n = 0, len(src)
    line = 1

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = src[i]

        if c == "\n":
            line += 1
            i += 1
            continue

        # ---- line comment, possibly a doc comment ----
        if src.startswith("//", i):
            start, start_line = i, line
            is_doc = src.startswith("///", i) and not src.startswith("////", i)
            j = src.find("\n", i)
            j = n if j < 0 else j
            if is_doc:
                # gather the whole run of /// lines
                pieces = [src[i + 3:j]]
                k = j
                while k < n:
                    m = re.match(r"[ \t]*///(?!/)([^\n]*)", src[k + 1:])
                    if not m:
                        break
                    pieces.append(m.group(1))
                    nxt = src.find("\n", k + 1)
                    if nxt < 0:
                        k = n - 1
                        break
                    k = nxt
                end = k + 1 if k < n else n
                docs.append(DocBlock("\n".join(pieces), start_line, end))
                blank(start, end)
                line += src.count("\n", start, end)
                i = end
                continue
            blank(start, j)
            i = j
            continue

        # ---- block comment, NESTING, possibly a doc comment ----
        if src.startswith("/*", i):
            start, start_line = i, line
            is_doc = src.startswith("/**", i) and not src.startswith("/**/", i)
            depth = 1
            j = i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth += 1
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if is_doc:
                body = src[start + 3:max(start + 3, j - 2)]
                body = "\n".join(re.sub(r"^\s*\*?", "", ln) for ln in body.split("\n"))
                docs.append(DocBlock(body, start_line, j))
            blank(start, j)
            line += src.count("\n", start, j)
            i = j
            continue

        # ---- raw string: #"..."#, ##"..."##, and their multiline forms ----
        if c == "#":
            m = re.match(r"#+", src[i:])
            hashes = m.group(0)
            after = i + len(hashes)
            if src.startswith('"""', after):
                close = '"""' + hashes
                j = src.find(close, after + 3)
                j = n if j < 0 else j + len(close)
            elif src.startswith('"', after):
                close = '"' + hashes
                j = src.find(close, after + 1)
                j = n if j < 0 else j + len(close)
            else:
                i += len(hashes)
                continue
            blank(i, j)
            line += src.count("\n", i, j)
            i = j
            continue

        # ---- multiline string ----
        if src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            blank(i, j)
            line += src.count("\n", i, j)
            i = j
            continue

        # ---- ordinary string, with interpolation and escapes ----
        if c == '"':
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    # `\(` opens interpolation: skip to its matching `)`
                    if j + 1 < n and src[j + 1] == "(":
                        depth, k = 1, j + 2
                        while k < n and depth:
                            if src[k] == "(":
                                depth += 1
                            elif src[k] == ")":
                                depth -= 1
                            k += 1
                        j = k
                        continue
                    j += 2
                    continue
                if src[j] == '"':
                    j += 1
                    break
                if src[j] == "\n":       # unterminated, do not run away
                    break
                j += 1
            blank(i, j)
            i = j
            continue

        i += 1

    return "".join(out), docs


# --------------------------------------------------------------------------
# The signature
# --------------------------------------------------------------------------

DECL = re.compile(r"\b(?P<kind>func|init|subscript)\b")


def _ident(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def walk(text: str):
    """Yield (index, char, paren_depth, angle_depth) over a declaration.

    Two Swift facts make a naive counter wrong, and both cost real findings:

      - `->` in a function type carries a `>` that is not a closing bracket.
        Counting it closed an angle level that was never opened, the depth
        went negative, and commas inside `(Key, Element, Element)` split the
        argument list. That is what turned two arguments of `keyed(by:)` in
        swift-algorithms into four;
      - generics nest: `init<R: Reducer<State, Action>>`. A non-greedy match
        up to the first `>` stops in the middle of the clause.

    So `->` is consumed as one atom, and `<` opens a level only where a type
    name could precede it.
    """
    par = ang = 0
    prev = ""
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        # Two-character operators that merely LOOK like brackets. `->` in a
        # function type, and the shift and comparison operators that turn up
        # in default values: `maxFrameSize: Int = 1 << 14` in swift-nio opened
        # an angle level that never closed, and the whole argument list after
        # it stopped splitting. Two of the four arguments of the WebSocket
        # upgrader went missing that way, and both were reported as findings.
        pair = text[i:i + 2]
        # ...but `>>` closes two generic levels when we are inside them, and
        # `init<R: Reducer<State, Action>>` ends exactly that way. Reading it
        # as a shift there left the clause open and turned nineteen sound
        # declarations in swift-composable-architecture into findings.
        atoms = ("->", "<=", ">=", "&&", "||") if ang else ("->", "<<", ">>", "<=", ">=", "&&", "||")
        if pair in atoms:
            yield i, text[i], par, ang
            yield i + 1, text[i + 1], par, ang
            i += 2
            prev = text[i - 1]
            continue
        if c in "([{":
            par += 1
        elif c in ")]}":
            par -= 1
        elif c == "<" and _ident(prev):
            ang += 1
        elif c == ">" and ang > 0:
            ang -= 1
        yield i, c, par, ang
        if not c.isspace():
            prev = c
        i += 1


def find_param_list(code: str, after: int) -> int:
    """Offset of the `(` that opens the parameter list, or -1.

    Walks past `?`/`!` of a failable init, the name or the operator, and a
    generic clause of any nesting depth.
    """
    i, n = after, len(code)
    while i < n and code[i] in " \t\n":
        i += 1
    if i < n and code[i] in "?!":
        i += 1
    while i < n and code[i] in " \t\n":
        i += 1
    m = re.match(r"`[^`]+`|[A-Za-z_]\w*|[-+*/%<>=!&|^~?]+", code[i:])
    if m and not code[i:].startswith("<"):
        i += m.end()
    while i < n and code[i] in " \t\n":
        i += 1
    if i < n and code[i] == "<":
        # Matched here rather than through walk(): walk opens an angle level
        # only where a type name precedes it, and the clause starts with the
        # `<` itself, so there is nothing in front of it to look at.
        depth, j = 0, i
        while j < n and j - i < 2000:
            c = code[j]
            if c == "-" and code[j + 1:j + 2] == ">":
                j += 2
                continue
            if c == "<":
                depth += 1
            elif c == ">":
                depth -= 1
                if depth == 0:
                    i = j + 1
                    break
            j += 1
        else:
            return -1
        if depth:
            return -1
        while i < n and code[i] in " \t\n":
            i += 1
    return i if i < n and code[i] == "(" else -1

# `@escaping`, `@autoclosure`, `@Sendable`, and modifiers that may precede a name
# `@escaping`, `@autoclosure`, and result builders that carry their own
# generics: `@ReducerBuilder<State, Action> _ build: () -> Reducers`. Missing
# the generic part left `<State, Action>` sitting where a name should be, and
# swift-composable-architecture reported it as an argument.
PARAM_NOISE = re.compile(
    r"^(?:@\w+(?:<[^>]*>)?(?:\([^)]*\))?\s*|inout\s+|borrowing\s+|consuming\s+|"
    r"isolated\s+|each\s+|__owned\s+|__shared\s+)+", re.S)


def matching_paren(text: str, start: int) -> int:
    """Offset just past the `)` matching the `(` at `start`, or -1."""
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def split_top(text: str) -> List[str]:
    """Split on commas outside every kind of bracket, `->` included."""
    parts, buf = [], []
    for _i, c, par, ang in walk(text):
        if c == "," and par == 0 and ang == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def names_of(param: str) -> Tuple[Optional[str], Optional[str]]:
    """(label, name) of one parameter, either of them possibly None.

    `from start: Point` -> ("from", "start")
    `_ person: String`  -> (None, "person")
    `person: String`    -> ("person", "person")
    """
    param = PARAM_NOISE.sub("", param.strip())
    # the type begins at the first colon outside every bracket
    cut = -1
    for i, c, par, ang in walk(param):
        if c == ":" and par == 0 and ang == 0:
            cut = i
            break
    head = (param[:cut] if cut >= 0 else param).strip()
    if not head:
        return None, None
    words = head.split()
    if len(words) >= 2:
        label = None if words[0] == "_" else words[0].strip("`")
        return label, words[1].strip("`")
    one = words[0].strip("`")
    return one, one


@dataclass
class Decl:
    kind: str
    line: int
    params: List[Tuple[Optional[str], Optional[str]]]
    text: str

    def accepts(self, documented: str) -> bool:
        """Swift gives an argument two names; either one may be documented."""
        for label, name in self.params:
            if documented == label or documented == name:
                return True
        return False

    @property
    def shown(self) -> str:
        out = []
        for label, name in self.params:
            if label and name and label != name:
                out.append(f"{label} {name}")
            else:
                out.append(name or label or "_")
        return ", ".join(out) if out else "(no arguments)"


def declarations(code: str) -> List[Decl]:
    """Every func / init / subscript in the blanked source, in file order."""
    found: List[Decl] = []
    for m in DECL.finditer(code):
        open_at = find_param_list(code, m.end())
        if open_at < 0:
            continue
        close = matching_paren(code, open_at)
        if close < 0 or close - open_at > MAX_PARAM_LIST:
            continue
        inner = code[open_at + 1:close - 1]
        found.append(Decl(
            kind=m.group("kind"),
            line=code.count("\n", 0, m.start()) + 1,
            params=[names_of(p) for p in split_top(inner)],
            text=re.sub(r"\s+", " ", code[m.start():close]).strip()[:200],
        ))
    return found


# --------------------------------------------------------------------------
# The doc comment
# --------------------------------------------------------------------------

ONE_PARAM = re.compile(r"^\s*[-*+]\s*[Pp]arameter\s+([`\w]+)\s*:", re.M)
PARAMS_HEAD = re.compile(r"^\s*[-*+]\s*[Pp]arameters\s*:\s*$", re.M)
SUB_ITEM = re.compile(r"^\s*[-*+]\s*([`\w]+)\s*:", re.M)
INDENTED_ITEM = re.compile(r"^([ \t]*)[-*+]\s*([`\w]+)\s*:", re.M)
ANY_ITEM = re.compile(r"^\s*[-*+]\s*(\w+)", re.M)


def documented_names(doc: str) -> List[str]:
    """Argument names a doc comment claims, from both spellings."""
    names = [m.group(1).strip("`") for m in ONE_PARAM.finditer(doc)]

    head = PARAMS_HEAD.search(doc)
    if head:
        tail = doc[head.end():]
        # the block ends at the first line that is not an indented sub-item
        block: List[str] = []
        for raw in tail.split("\n"):
            if not raw.strip():
                block.append(raw)
                continue
            if re.match(r"^\s*[-*+]\s*(?:[Pp]arameter|[Rr]eturns|[Tt]hrows|"
                        r"[Nn]ote|[Ww]arning|[Cc]omplexity|[Pp]recondition|"
                        r"[Pp]ostcondition|[Ii]mportant|[Aa]uthor|[Ss]ee[Aa]lso|"
                        r"[Ii]nvariant|[Rr]equires)\b", raw):
                break
            if re.match(r"^\s{2,}[-*+]\s", raw) or re.match(r"^\s*[-*+]\s", raw):
                block.append(raw)
                continue
            break
        # Only the shallowest level of the list names this function's
        # arguments. A deeper level documents the arguments of a CLOSURE that
        # one of them takes, and reading those as arguments of the function
        # is how `oldValue` and `state` of `onChange(of:_:)` were reported in
        # swift-composable-architecture:
        #
        #   - perform: A closure to run when the value changes.
        #     - `oldValue`: The old value that failed the check.
        #     - `state`:    The current, mutable state.
        items = [(len(m.group(1).expandtabs(4)), m.group(2).strip("`"))
                 for m in INDENTED_ITEM.finditer("\n".join(block))]
        if items:
            # The FIRST item sets the level of this list. An item deeper than
            # it belongs to a closure argument; an item shallower has left the
            # list altogether, which is how NIOCore/Codec.swift writes a
            # malformed `- return:` right under `- Parameters:`. Taking the
            # minimum indent as the base read that one as an argument and threw
            # the real one away.
            base = items[0][0]
            for indent, name in items:
                if indent < base:
                    break
                if indent == base:
                    names.append(name)
    return names


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------

@dataclass
class Finding:
    path: str
    line: int
    kind: str
    documented: str
    signature: str
    decl: str


@dataclass
class Report:
    files: int = 0
    files_skipped: int = 0
    decls: int = 0
    doc_blocks: int = 0
    doc_with_params: int = 0
    unattached: int = 0
    both_names_ok: int = 0
    findings: List[Finding] = dc_field(default_factory=list)


def check_tree(root: str, report: Report) -> None:
    for dirpath, _dirs, names in common.walk(root):
        for n in sorted(names):
            if not n.endswith(".swift"):
                continue
            path = os.path.join(dirpath, n)
            src = common.read_text(path)
            if not src:
                report.files_skipped += 1
                continue
            report.files += 1
            rel = os.path.relpath(path, root)
            code, docs = scan(src)
            decls = declarations(code)
            report.decls += len(decls)
            report.doc_blocks += len(docs)
            by_line = sorted(decls, key=lambda d: d.line)

            for doc in docs:
                wanted = documented_names(doc.text)
                if not wanted:
                    continue
                report.doc_with_params += 1
                # the declaration this comment belongs to: the first one that
                # starts at or after the end of the comment
                doc_end_line = doc.line + doc.text.count("\n")
                target = next((d for d in by_line if d.line > doc_end_line), None)
                if target is None or target.line > doc_end_line + 6:
                    report.unattached += 1
                    continue
                for name in wanted:
                    if target.accepts(name):
                        report.both_names_ok += 1
                        continue
                    report.findings.append(Finding(
                        path=rel, line=target.line, kind=target.kind,
                        documented=name, signature=target.shown, decl=target.text))


def print_report(root: str, report: Report, verbose: bool) -> None:
    if report.findings:
        print(f"=== Documented name is neither a label nor a parameter "
              f"({len(report.findings)}) ===\n")
        for f in report.findings:
            print(f"  {f.path}:{f.line}  {f.kind}")
            print(f"    in the comment:     {f.documented}")
            print(f"    in the signature:   {f.signature}")
            if verbose:
                print(f"    declaration:        {f.decl}")
            print()

    print("=== Coverage ===")
    print(f"  tree:                   {root}")
    print(f"  files read:             {report.files}")
    print(f"  files skipped:          {report.files_skipped} (empty or too large)")
    print(f"  declarations found:     {report.decls} (func, init, subscript)")
    print(f"  doc comments:           {report.doc_blocks}")
    print(f"  of those naming args:   {report.doc_with_params}")
    print(f"  comment with no decl:   {report.unattached} (nothing within six lines below)")
    print(f"  name matched:           {report.both_names_ok} (as a label or as a parameter name)")
    print(common.findings_line(len(report.findings), 0))
    print(f"  run:                    {TOOL}.py fingerprint "
          f"{abs(hash((root, len(report.findings), report.decls))) % (16 ** 8):08x}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Swift doc comment against the signature")
    ap.add_argument("tree", help="directory to read")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.tree))
    if not os.path.isdir(root):
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    report = Report()
    check_tree(root, report)
    print_report(root, report, args.verbose)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump([f.__dict__ for f in report.findings], fh,
                      ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
