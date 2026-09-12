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

TWO SYNTAXES. Doxygen writes `\\param name text`; gtk-doc writes `@name: text`
under a line naming the symbol. harfbuzz, GLib, GTK, GStreamer, pango and
libsoup use the second, and to a Doxygen-only tool they all read as
documentation-free: 336 headers of harfbuzz, "0 blocks with \\param". A gtk-doc
block also documents the PUBLIC prototype and sits next to the body, so for
that syntax the headers are indexed first and the comment is compared against
the declaration from the header rather than against the definition below it --
harfbuzz names an argument `coords_length` in hb-font.h and
`input_coords_length` in hb-font.cc, and the block is right to follow the
header.

KNOWN BLIND SPOTS:
  - **headers, plus implementation files for gtk-doc only.**
    `.hpp .h .hh .hxx .h++ .ipp .inl` are read in full; `.c .cc .cpp .cxx` are
    read only for gtk-doc blocks, because that is where such a project keeps
    them. Doxygen in a `.cc` stays unread, so no other project's report moves.
    Until 06.08.2026 only `.hpp` was read, which made every `.h` project report
    a clean zero -- protobuf 6 headers of 611, abseil 0 of 385;
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

# A declaration ends at `{`, at `;`, or -- for a constructor -- at the colon
# that opens the member-initializer list. Without that third ending the
# argument list plus the initializers routinely runs past the 900-character
# limit, the match slides forward, and the comment gets compared against the
# NEXT declaration in the file. On PCL that turned one constructor docblock
# into 8 false findings against the destructor below it (issue #6).
DECL_END = r'(?:\{|;|(?<=\))\s*:(?!:))'
BLOCK = re.compile(r'/\*[!*](.*?)\*/\s*(.{0,900}?)' + DECL_END, re.S)
SLASH = re.compile(r'((?:^[ \t]*///[^\n]*\n)+)([\s\S]{0,900}?)' + DECL_END, re.M)
PARAM = re.compile(r'[\\@]param\s*(?:\[[^\]]*\])?\s+([A-Za-z_]\w*)')
TPARAM = re.compile(r'[\\@]tparam\s+([A-Za-z_]\w*)')

# gtk-doc says the same thing in another syntax: the block opens with the name
# of the symbol on a line of its own, and each parameter is `@name: text`
# rather than `\param name text`. A whole family of root libraries writes this
# way -- harfbuzz, GLib, GTK, GStreamer, pango, libsoup -- and to a tool that
# knows only Doxygen they all read as documentation-free. On harfbuzz that was
# 336 headers read and "0 blocks with \param", which is the shape of blindness
# rather than of cleanliness.
GTK_HEAD = re.compile(r'\A(?:\s*\*?\s*\n)*\s*\*?\s*([A-Za-z_]\w*)\s*:(?:\s*\([^)]*\))?\s*$', re.M)
GTK_PARAM = re.compile(r'^\s*\*\s*@([A-Za-z_]\w*)\s*:', re.M)
# Fields of the block itself. `Returns:` and `Since:` also appear as `@Returns:`
# in older sources, so they are matched case-insensitively below.
GTK_NOT_A_PARAM = frozenset({
    "title", "short_description", "include", "see_also", "stability",
    "section_id", "image", "returns", "return", "since", "deprecated",
})


def gtk_params(doc: str) -> List[str]:
    """Parameters of a gtk-doc block, or nothing when the block is not one."""
    head = GTK_HEAD.match(doc)
    if not head or head.group(1) == "SECTION":
        return []
    return [n for n in GTK_PARAM.findall(doc) if n.lower() not in GTK_NOT_A_PARAM]


def gtk_symbol(doc: str) -> Optional[str]:
    """The symbol a gtk-doc block documents, from its opening line."""
    head = GTK_HEAD.match(doc)
    return head.group(1) if head else None


# A function-like macro is documented the same way as a function, and the
# preprocessor stripper removes the `#define` right after the block, so the
# comment ends up compared against the NEXT real declaration in the file. In
# harfbuzz that is `HB_TAG(c1,c2,c3,c4)` measured against `hb_tag_from_string
# (str, len)`: four false findings from one macro, and the file has more.
MACRO_DEF = re.compile(r'^\s*#\s*define\s+([A-Za-z_]\w*)\(([^)]*)\)', re.M)


def macro_params(decl: str, symbol: Optional[str]):
    """Parameters of the macro this block documents, when that is what it is."""
    for m in MACRO_DEF.finditer(decl):
        if symbol is not None and m.group(1) != symbol:
            continue
        return [p.strip() for p in m.group(2).split(",") if p.strip()]
    return None


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


def _angle_depth(decl, upto):
    """How many unclosed '<' stand before position `upto`.

    Only those that are template brackets: `operator<` and a shift keep their
    angle brackets in code, so a '<' with an operand on both sides is not a
    bracket. Cheap approximation, and it is the one that matters here.
    """
    depth = 0
    for k in range(upto):
        c = decl[k]
        if c == '<' and (k + 1 >= len(decl) or decl[k + 1] not in '<='):
            depth += 1
        elif c == '>' and (k == 0 or decl[k - 1] not in '->'):
            depth = max(0, depth - 1)
    return depth


def _find_call_paren(decl, start):
    """The first parenthesis that opens an argument list rather than a macro.

    Three things sit in parentheses that are not the argument list, and all
    three were reported from the field (issue #6, PCL, 24 false findings of 28
    read by hand):

      - a macro in the return type: BOOST_BEAST_ASYNC_RESULT2(Handler)
      - a keyword: noexcept(...), alignas(...)
      - a return type carrying its own call signature:
        `std::function<void (ScalarType)> f(const std::string& name)`

    The third is the one this reads by bracket depth. Inside `<...>` nothing
    can be an argument list, because that is a template argument.
    """
    i = decl.find('(', start)
    while i >= 0:
        head = decl[start:i]
        inside_template = _angle_depth(decl, i) > 0
        if (not inside_template
                and not MACRO_BEFORE_PAREN.search(head)
                and not KEYWORD_BEFORE_PAREN.search(head)):
            return i
        # skip over the whole macro body, or over the parentheses of a type
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
# `T::*callback` carries no star before the class name, so a pattern demanding
# [*&] first misses the member-function-pointer spelling entirely: reported
# from the field as 3 false findings on PCL (issue #6). The star belongs after
# the optional qualification, not before it.
DECLARATOR = re.compile(
    r'^[^(),]*?(?:[A-Za-z_]\w*\s*::\s*)*[*&]\s*[A-Za-z_]\w*\s*$')
# A bare name in parentheses is a declarator too. C libraries write the
# definition that way when a macro of the same name would otherwise expand:
# `uint8_t (hb_color_get_alpha) (hb_color_t color)`. Five false findings in
# harfbuzz, all of them reported as "in the declaration: (empty)", because the
# name was read as the argument list and the list held no names.
BARE_NAME = re.compile(r'^\s*[A-Za-z_]\w*\s*$')
OPENS_NEXT = re.compile(r'\s*\(')

# A parameter written as its type alone has no name to match a \param against.
BUILTIN_TYPES = frozenset({
    "void", "bool", "char", "short", "int", "long", "float", "double",
    "signed", "unsigned", "size_t", "ssize_t", "auto", "wchar_t",
    "char8_t", "char16_t", "char32_t", "nullptr_t",
})


def _is_declarator(inner, decl, close):
    """Do these parentheses hold a name rather than an argument list.

    Two things have to hold at once, and the second is what keeps an ordinary
    one-argument function safe: `void f(int *x)` looks exactly like a declarator
    on the inside, and is told apart by what follows the closing parenthesis.
    A declarator is always followed by the argument list itself.
    """
    if not OPENS_NEXT.match(decl[close + 1:]):
        return False
    if DECLARATOR.match(inner):
        return True
    # `(name) (args)`: a bare name is a declarator, a bare TYPE is a nameless
    # argument. Only one of the two is followed by another argument list, and
    # that has already been checked above.
    return bool(BARE_NAME.match(inner)) and inner.strip() not in BUILTIN_TYPES


def _has_arguments(decl):
    """Are the argument brackets non-empty, whatever is written inside."""
    i = _find_call_paren(decl, _skip_template(decl))
    if i < 0:
        return False
    j = _match_paren(decl, i)
    return j > i + 1 and bool(decl[i + 1:j].strip())


def has_unnamed_params(decl):
    """Does the declaration carry a parameter written as its type alone.

    `void f(bool = false)` and `void f(int, double)` have arguments with no
    name, so a \\param can never match them. That is a defect in the
    declaration rather than in the comment, and it is not ours to send.
    """
    i = _find_call_paren(decl, _skip_template(decl))
    if i < 0:
        return False
    j = _match_paren(decl, i)
    if j < 0:
        return False
    inner = decl[i + 1:j]
    if not inner.strip():
        return False
    depth, cur, parts = 0, "", []
    for ch in inner:
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    parts.append(cur)
    for part in parts:
        head = part.split("=")[0].strip()
        words = re.findall(r"[A-Za-z_]\w*", head)
        if len(words) == 1 and words[0] in BUILTIN_TYPES:
            return True
    return False


def sig_params(decl):
    """Argument identifiers taken from the parentheses of a declaration."""
    # A comment inside the argument list is not part of any name. harfbuzz
    # annotates directions there -- `hb_tag_t *table_tags /* OUT */` -- and the
    # name was read as `OUT`, so every such argument looked undocumented.
    decl = re.sub(r'/\*.*?\*/', ' ', decl, flags=re.S)
    decl = re.sub(r'//[^\n]*', ' ', decl)
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
        # array by reference and function pointer: char(&dest)[N], void(*cb)(int),
        # and the member spelling void (T::*callback)(...) -- the qualification
        # sits between the bracket and the star, so it has to be allowed here
        # too, or the name is read from the pointer's own argument list
        m2 = re.search(r'\(\s*(?:[A-Za-z_]\w*\s*::\s*)*[&*]\s*([A-Za-z_]\w*)\s*\)', n)
        if m2:
            COUNTS["fnptr_arg"] = COUNTS.get("fnptr_arg", 0) + 1
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
        # The same tail without parentheses: an attribute macro right after the
        # name, `const hb_gpu_draw_t *draw HB_UNUSED`. Elsewhere it is spelled
        # G_GNUC_UNUSED or UNUSED. Read as the name, it makes every argument so
        # marked look undocumented. Same shape as the rule above, so the same
        # length bound: four characters or more, all upper case.
        attr = re.sub(r'\b[A-Z][A-Z0-9_]{3,}\s*$', '', n).strip()
        if attr and re.search(r'[A-Za-z_]\w*', attr):
            n = attr
        # Nothing after the decoration means no name: `constraint_t<...>`,
        # `PointCloudOut &`, `const pcl::PointCloud<PointSource>&`. Taking the
        # last word there picks a piece of the TYPE -- on PCL that read
        # `PointSource` out of the template argument and reported it as the
        # parameter (issue #6).
        if not n or n.rstrip().endswith(('>', '&', '*')):
            if n:
                COUNTS["unnamed"] = COUNTS.get("unnamed", 0) + 1
            continue
        m = re.findall(r'[A-Za-z_]\w*', n)
        if not m:
            continue
        # An unnamed parameter: `void setNonMaxSupression (bool = false)`. The
        # last word is the type, so taking it as the name turns a documented
        # `nonmax` into a mismatch against `bool`. The docstring here is not
        # wrong -- the declaration simply has nowhere to attach it, and fixing
        # that means naming the parameter, which is a code change and not
        # something a docstring pass should send. Reported from the field on
        # PCL harris_2d/3d/6d (issue #6).
        # One word is a type with nowhere to hang a name, whether it is a
        # keyword (`bool = false`) or a user type (`PointCloudOut &`). The
        # keyword list alone missed the second, found on PCL don.h:125.
        if len(m) == 1:
            COUNTS["unnamed"] = COUNTS.get("unnamed", 0) + 1
            continue
        out.append(m[-1])
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
                          "aliases": 0, "fnptr": 0, "gtk": 0, "sources": 0,
                          "macro": 0}


def scan_text(src: str, rel: str, gtk_only: bool = False) -> List[dict]:
    """Findings in one header. A separate function so tests need no disk.

    `gtk_only` is for implementation files. gtk-doc keeps the documentation of
    a function next to its body rather than next to the declaration, so those
    files have to be read too -- but only for that syntax, so that what the
    tool reports on every other project stays exactly as it was.
    """
    hits: List[dict] = []
    for m in itertools.chain(BLOCK.finditer(src), SLASH.finditer(src)):
        doc, raw_decl = m.group(1), m.group(2)
        decl = _strip_preprocessor(raw_decl)
        pnames = PARAM.findall(doc)
        tnames = TPARAM.findall(doc)
        is_gtk = False
        if not pnames:
            pnames = gtk_params(doc)
            is_gtk = bool(pnames)
        if gtk_only and not is_gtk:
            continue
        if not pnames and not tnames:
            continue
        COUNTS["gtk"] += 1 if is_gtk else 0
        # The block may be documenting a function-like macro rather than the
        # declaration that follows it.
        mp = macro_params(raw_decl, gtk_symbol(doc) if is_gtk else None)
        if mp is not None:
            COUNTS["blocks"] += 1
            COUNTS["macro"] += 1
            ln = line_of(src, m.start())
            for n in pnames:
                if n not in mp:
                    hits.append(dict(kind='param', hard=True, file=rel, line=ln,
                                     name=n, sig=mp, unnamed=False,
                                     decl=' '.join(raw_decl.split())[:120]))
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
        # A gtk-doc block documents the public prototype. When the headers
        # declare this symbol, they are the thing to compare against: the
        # definition below the block may name its arguments differently, and
        # that is legal C rather than a documentation defect.
        # Only when the block really sits above a function. A gtk-doc block
        # over `typedef enum {...} hb_paint_extend_t` documents the members of
        # the enum with the same `@NAME:` syntax, and there is no argument list
        # anywhere near: 62 findings appeared out of nowhere when the public
        # prototype was substituted into those too.
        if is_gtk:
            symbol = gtk_symbol(doc) or ""
            public = HEADER_SIGS.get(symbol)
            if public and sp is not None:
                sp = public
                COUNTS["public"] = COUNTS.get("public", 0) + 1
            elif symbol != declared_name(decl):
                # The block names the symbol it documents, and this one is not
                # the declaration below it: a block over `SoupCookieJarAcceptPolicy`
                # followed by `soup_cookie_jar_set_accept_policy` documents the
                # members of an enum, and every member read as a parameter.
                # 67 findings on libsoup, 260 on glib, all of this shape.
                COUNTS["other_symbol"] = COUNTS.get("other_symbol", 0) + 1
                continue
        tp = tpl_params(decl)
        ln = line_of(src, m.start())
        if sp is not None:
            # `void setNonMaxSupression (bool = false)` documents `nonmax` and
            # the declaration has nowhere to attach it. The docstring is not
            # wrong; the parameter simply has no name. Fixing that means
            # editing the signature, which is a code change and not what a
            # documentation pass should send, so this drops to soft.
            # Two ways a declaration ends up with nothing to attach a
            # \param to: some arguments are written as bare types, or all of
            # them are -- the latter is what `using Signature = void(A, B)`
            # looks like. Either way the comment is not wrong and the fix is a
            # code change, so the finding drops to soft rather than vanishing.
            unnamed = has_unnamed_params(decl) or (not sp and _has_arguments(decl))
            for n in pnames:
                if n not in sp:
                    hits.append(dict(kind='param', hard=not unnamed, file=rel, line=ln,
                                     name=n, sig=sp, unnamed=unnamed,
                                     decl=' '.join(decl.split())[:120]))
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

# Implementation files, read for gtk-doc only. A gtk-doc block sits above the
# body of the function, so a project written that way keeps almost all of its
# documentation here and none of it in the headers.
SOURCE_GLOBS = ("*.c", "*.cc", "*.cpp", "*.cxx")


def _files(base: pathlib.Path, globs):
    seen = set()
    for pattern in globs:
        for p in base.rglob(pattern):
            if p not in seen:
                seen.add(p)
                yield p


def _headers(base: pathlib.Path):
    yield from _files(base, HEADER_GLOBS)


# Public prototypes, gathered from the headers before the sources are read.
#
# gtk-doc documents the PUBLIC interface, and a C library is free to name the
# arguments of a definition differently from its declaration. harfbuzz does:
# `hb_font_set_var_coords_design` takes `coords_length` in hb-font.h and
# `input_coords_length` in hb-font.cc. The block, correctly, documents the
# header. Comparing it against the definition it happens to sit above reported
# three findings that were the tool's error rather than the project's.
HEADER_SIGS: Dict[str, List[str]] = {}
# A real class declaration, not the word in a comment. Matching the bare word
# threw away pango-layout.h, which says "class" in prose about font classes,
# and with it every prototype spelling `index_` -- 8 false findings that named
# the very difference the header exists to declare.
CXX_CLASS = re.compile(r'^\s*(?:template\s*<[^>]*>\s*)?class\s+\w+', re.M)
NAME_BEFORE_PAREN = re.compile(r'([A-Za-z_]\w*)\s*$')


def declared_name(decl: str) -> Optional[str]:
    """The name a declaration declares: the word before its argument list."""
    i = _find_call_paren(decl, _skip_template(decl))
    if i < 0:
        return None
    m = NAME_BEFORE_PAREN.search(decl[:i].rstrip().rstrip(")").rstrip())
    return m.group(1) if m else None


def _remember_header_sigs(src: str) -> None:
    """Every prototype the header declares, documented or not.

    Reading only the declarations that carry a docblock finds nothing here:
    a gtk-doc project documents in the .cc and leaves the header bare.
    """
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.S)
    for chunk in src.split(';'):
        if '(' not in chunk or '{' in chunk:
            continue
        # A `#define` above the prototype carries no `;`, so it lands in the
        # same chunk. Dropping the whole chunk over it lost g_array_new and
        # every other prototype that follows a macro block in the header.
        decl = _strip_preprocessor(chunk).strip()
        # `GIOStatus (*io_write) (GIOChannel *channel, ...)` is a field of a
        # struct of callbacks, not a prototype. Indexed as one, it taught the
        # tool that the TYPE `GIOStatus` takes those arguments, and every
        # gtk-doc block about that enum was then measured against them.
        if '(' not in decl or '(*' in decl.replace(' ', ''):
            continue
        if len(decl) > 900:
            continue
        name = declared_name(decl)
        if not name:
            continue
        params = sig_params(decl)
        if not params:
            continue
        known = HEADER_SIGS.get(name)
        if known is None:
            HEADER_SIGS[name] = params
        elif known != params:
            # Two prototypes of one name that disagree: nothing reliable to
            # compare against, so the tool keeps quiet rather than guesses.
            HEADER_SIGS[name] = []


def scan(root: str) -> List[dict]:
    COUNTS.update(files=0, blocks=0, glued=0, skipped=0, fnptr=0, gtk=0, sources=0, macro=0)
    HEADER_SIGS.clear()
    hits: List[dict] = []
    base = pathlib.Path(root)

    def read(p):
        parts = p.relative_to(base).parts
        if any(x in SKIP_DIRS or (x.startswith(".") and x not in common.KEEP_HIDDEN)
               for x in parts):
            return None
        try:
            return p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            COUNTS["skipped"] += 1
            return None

    for p in sorted(_headers(base)):
        src = read(p)
        if src is None:
            continue
        COUNTS["files"] += 1
        # The public C interface only. harfbuzz keeps its C++ internals in .hh
        # with methods of the same names, and mixing the two indexes gave 12
        # findings where a comment was measured against an unrelated signature.
        if p.suffix == ".h" and not CXX_CLASS.search(src):
            _remember_header_sigs(src)
        hits.extend(scan_text(src, str(p.relative_to(base))))

    for p in sorted(_files(base, SOURCE_GLOBS)):
        src = read(p)
        if src is None:
            continue
        COUNTS["sources"] += 1
        hits.extend(scan_text(src, str(p.relative_to(base)), gtk_only=True))
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
    print(f"  sources read:           {COUNTS.get('sources', 0)} (gtk-doc lives next to the body)")
    print(f"  headers skipped:        {COUNTS['skipped']} (unreadable)")
    print(f"  blocks with \\param:     {COUNTS['blocks']}")
    print(f"  of them gtk-doc:        {COUNTS.get('gtk', 0)} (@name: rather than \\param name)")
    print(f"  macro blocks:           {COUNTS.get('macro', 0)} (compared against #define, not the next declaration)")
    print(f"  checked against header: {COUNTS.get('public', 0)} (gtk-doc documents the public prototype)")
    print(f"  blocks about another symbol:{COUNTS.get('other_symbol', 0)} (an enum or a struct, not the declaration below)")
    if engine == "clang":
        print(f"  headers the compiler parsed: {COUNTS['clang_parsed']}")
        print(f"  headers it could not:   {COUNTS['clang_failed']}")
        print(f"  stub headers created:   {COUNTS['stubs']} (empty, for missing includes)")
        print(f"  project aliases skipped:{COUNTS['aliases']} (\\param_name from the project Doxyfile)")
    else:
        print(f"  family blocks:          {COUNTS['glued']} (a name repeats, cannot judge)")
        # Two different shapes, and reporting only the first read as "there are
        # none" when the tree was full of the second (issue #6).
        print(f"  fn pointer declarations:{COUNTS['fnptr']} (the NAME is in parentheses, arguments follow)")
        print(f"  fn pointer arguments:   {COUNTS.get('fnptr_arg', 0)} (void(*cb)(int), char(&d)[N], void (T::*m)())")
    # Was `findings_line(len(hits), 0)`: every finding counted as hard, no
    # matter what the scan decided. The same lie as in the JSON writer, and it
    # survived because until now there was no soft class to get wrong.
    hard = sum(1 for h in hits if h.get("hard", True))
    soft = len(hits) - hard
    if soft:
        print(f"  unnamed parameters:     {soft} "
              f"(the declaration has no name to attach a \\param to)")
    print(common.findings_line(hard, soft))
    print(stamp.line(__file__, ["common.py"]))



# ----------------------------------------------------------------------
# Движок decl: сравнивает объявление с его же определением.
# Возвращён из вольтовой линии 18.08.2026 при сведении двух копий.
# ----------------------------------------------------------------------

CALLABLE = re.compile(r'(?<![\w:])([A-Za-z_]\w*)\s*\(')
QUALIFIER = re.compile(r'^(__)?restrict(__)?$|_restrict$')
BLOCK_COMMENT = re.compile(r'/\*.*?\*/|//[^\n]*', re.S)
STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"' r"|'(?:\\.|[^'\\])*'")


def _arg_names(inner: str) -> list:
    """Argument names with `None` where the argument carries no name."""
    parts, depth, cur = [], 0, ''
    for ch in inner:
        if ch in '<([{':
            depth += 1
        elif ch in '>)]}':
            depth -= 1
        if ch == ',' and depth == 0:
            parts.append(cur)
            cur = ''
        else:
            cur += ch
    parts.append(cur)

    names = []
    for part in parts:
        part = part.split('=')[0].strip()
        if not part or part in ('void', '...'):
            continue
        m2 = re.search(r'\(\s*[&*]\s*([A-Za-z_]\w*)\s*\)', part)
        if m2:                      # function pointer: void (*cb)(int)
            names.append(m2.group(1))
            continue
        bare = re.sub(r'\[[^\]]*\]', '', part)
        # an attribute macro sits after the name and would be taken for it:
        # `gpointer data G_GNUC_UNUSED`, `hb_font_t *font HB_UNUSED`. On glib
        # alone this produced 288 findings, every one of them G_GNUC_UNUSED.
        bare = re.sub(r'(\s[A-Z][A-Z0-9_]{2,})+\s*$', '', bare)
        words = re.findall(r'[A-Za-z_]\w*', bare)
        if not words:
            names.append(None)
            continue
        taken = words[-1]
        names.append(None if _looks_unnamed(bare, taken) else taken)
    return names

def _blank_comments(src: str) -> str:
    """Comments and literals are replaced by spaces of the same length so that
    line numbers stay correct."""
    def blank(m):
        return ''.join(' ' if ch != '\n' else '\n' for ch in m.group(0))
    return STRING_LITERAL.sub(blank, BLOCK_COMMENT.sub(blank, src))

def _callables_in(src: str):
    """(name, argument names, 'declaration' | 'definition', position)."""
    src = _blank_comments(src)
    for m in CALLABLE.finditer(src):
        name = m.group(1)
        if name in TYPE_WORDS or name in ('if', 'for', 'while', 'switch',
                                          'return', 'sizeof', 'defined'):
            continue
        open_paren = m.end() - 1
        close = _match_paren(src, open_paren)
        if close < 0:
            continue
        tail = src[close + 1: close + 200].lstrip()
        # a macro-shaped name is a macro, not a function
        if re.fullmatch(r'[A-Z][A-Z0-9_]*', name):
            continue
        if tail.startswith(';'):
            kind = 'declaration'
        elif tail.startswith('{'):
            kind = 'definition'
        else:
            continue                # a call, an attribute, anything else
        inner = src[open_paren + 1: close]
        # A call also ends in a semicolon: `line_iter_next_cluster (it, gap);`
        # looked exactly like a prototype and paired with the real definition,
        # 59 findings on pango alone. Two things tell them apart: a declaration
        # is preceded by a return type inside its own statement, and its
        # arguments carry types rather than bare variable names.
        if kind == 'declaration' and not _looks_declared(src, m.start(), inner):
            COUNTS["decl_calls"] += 1
            continue
        args = _arg_names(inner)
        if not args:
            continue
        yield name, args, kind, m.start()

def _looks_declared(src: str, name_at: int, inner: str) -> bool:
    head = src[:name_at]
    cut = max(head.rfind(';'), head.rfind('{'), head.rfind('}'), head.rfind(')'))
    prefix = head[cut + 1:]
    if not re.search(r'[A-Za-z_]\w*', prefix):
        return False                # nothing before the name: a bare call
    if re.search(r'(=|\breturn\b|,|\|\||&&|\?|:)\s*$', prefix):
        return False                # the name stands in an expression
    # At least one argument has to carry a type. An access operator rules that
    # out: `categories[i].opt` and `async->thrdd.res_A` have several words each
    # and passed the earlier version of this test, so three calls in curl came
    # out as prototypes.
    for a in inner.split(','):
        a = a.strip()
        if not a or a == 'void':
            continue
        if any(op in a for op in ('.', '->', '[', '(')):
            continue
        if len(re.findall(r'[A-Za-z_]\w*', a)) > 1 or '*' in a or '&' in a:
            return True
    return False

def _looks_unnamed(argument: str, taken: str) -> bool:
    """True when `taken` is really the tail of a type, not an argument name.

    `const char *` yields `char`, `struct stat *` yields `stat`, `GFile *`
    yields `GFile`. All three are unnamed arguments and must not be compared.

    A whole family of projects declares functions with types only and no names
    at all. ImageMagick writes `GetPixelCacheColorspace(const Cache)` and
    `FormatMagickCaption(Image *,DrawInfo *,const MagickBooleanType,...)`, so
    the counting has to ignore qualifiers: `const Cache` is two words but one
    of them is `const`, and what is left is a bare type. Forty-seven findings
    on ImageMagick, `declares Cache` and `declares MagickBooleanType` among
    them, all of this nature.
    """
    if taken in BASE_TYPES or taken.endswith("_t") or QUALIFIER.search(taken):
        return True
    # Qualifiers belong to neither the type nor the name and are dropped first.
    # What is left is one word for a bare type and two for a type plus a name:
    #   const Cache              -> [Cache]           unnamed
    #   const uint8_t *buf       -> [uint8_t, buf]    named
    #   NexusInfo *magick_restrict -> [NexusInfo]     unnamed
    words = [w for w in re.findall(r'[A-Za-z_]\w*', argument)
             if w not in QUALIFIERS and not QUALIFIER.search(w)]
    if len(words) <= 1:
        return True
    # the name never carries the pointer or reference: in `GFile *file` the
    # last token follows a star, in `GFile *` it does not
    after = argument[argument.rfind(taken) + len(taken):]
    if after.strip() not in ("", "[]"):
        return True
    return False

def collect_callables(root: str, counts: dict) -> tuple:
    """Declarations and definitions of every function in the tree."""
    declared, defined = {}, {}
    base = pathlib.Path(root)
    for path in sorted(base.rglob('*')):
        # C only. In C++ the same shapes mean other things: a templated call
        # reads as a prototype, a member of a class collides with a free
        # function of the same name, and `declares this` or `declares nullptr`
        # comes out the far end. Measured on harfbuzz: 62 findings, nearly all
        # of that nature. C++ needs the compiler, not a regular expression.
        if path.suffix not in ('.h', '.c'):
            continue
        parts = path.relative_to(base).parts
        if any(x in SKIP_DIRS or (x.startswith(".") and x not in common.KEEP_HIDDEN)
               for x in parts):
            continue
        try:
            src = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        # A generated file is not ours to edit: the fix belongs in the grammar
        # it came from. jq keeps `src/lexer.c` next to `src/lexer.l`, and three
        # findings sat in the generated one.
        head = src[:400]
        if '#line' in head or 'generated by' in head.lower() or 'DO NOT EDIT' in head:
            counts["decl_generated"] += 1
            continue
        # A C++ header declares functions that live in another world: the C++
        # `CloneString(std::string&)` of Magick++ paired with the C
        # `CloneString(char **)` of MagickCore purely by name.
        if re.search(r'^\s*(class|namespace)\s+\w', src, re.M):
            counts["decl_cpp"] += 1
            continue
        counts["decl_files"] += 1
        rel = str(path.relative_to(base))
        for name, args, kind, pos in _callables_in(src):
            store = defined if kind == 'definition' else declared
            store.setdefault((name, len(args)), []).append(
                dict(file=rel, line=line_of(src, pos), args=args))
    return declared, defined

def scan_decl(root: str) -> List[dict]:
    COUNTS.update(decl_files=0, decl_pairs=0, decl_ambiguous=0, decl_unnamed=0,
                  decl_calls=0, decl_generated=0, decl_cpp=0)
    declared, defined = collect_callables(root, COUNTS)
    hits: List[dict] = []
    for key, definitions in defined.items():
        declarations = declared.get(key)
        if not declarations:
            continue
        # overloads and same-arity twins cannot be paired by name alone
        if len(declarations) > 1 or len(definitions) > 1:
            COUNTS["decl_ambiguous"] += 1
            continue
        head, body = declarations[0], definitions[0]
        COUNTS["decl_pairs"] += 1
        for i, (a, b) in enumerate(zip(head["args"], body["args"])):
            if a is None or b is None:
                COUNTS["decl_unnamed"] += 1
                continue
            if a != b:
                hits.append(dict(
                    kind='decl', hard=True, file=head["file"], line=head["line"],
                    name=a, sig=[b], where=key[0], position=i + 1,
                    decl=f'{head["file"]}:{head["line"]} declares {a}, '
                         f'{body["file"]}:{body["line"]} defines {b}'))
    return hits

def print_decl_report(hits: List[dict], root: str, verbose: bool = False) -> None:
    if hits:
        print(f"\n=== An argument named one way in the header and another in the "
              f"source ({len(hits)}) ===")
        for h in hits[: (len(hits) if verbose else 40)]:
            print(f"\n  {h['where']}(), argument {h['position']}")
            print(f"    {h['decl']}")
        if not verbose and len(hits) > 40:
            print(f"\n  ... {len(hits) - 40} more, use -v for all")

    print("\n=== Coverage ===")
    print(f"  tree:                   {root}")
    print(f"  engine:                 decl")
    print(f"  files read:             {COUNTS['decl_files']}")
    print(f"  declaration/definition pairs: {COUNTS['decl_pairs']}")
    print(f"  ambiguous, skipped:     {COUNTS['decl_ambiguous']} (overloads: one name, several arities)")
    print(f"  arguments with no name: {COUNTS['decl_unnamed']} (a header may omit them)")
    print(f"  calls read as prototypes:{COUNTS['decl_calls']} (dismissed: no return type before the name)")
    print(f"  generated files skipped:{COUNTS['decl_generated']} (fix belongs in the grammar)")
    print(f"  C++ files skipped:      {COUNTS['decl_cpp']} (another world, same names)")
    print(common.findings_line(len(hits), 0))
    print(stamp.line(__file__, ["common.py"]))

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Doxygen \\param against the C++ signature")
    ap.add_argument("root", help="directory holding the headers")
    ap.add_argument("--engine", choices=("regex", "clang", "decl"), default="regex",
                    help="regex reads the text, clang asks the compiler, "
                         "decl compares a declaration with its own definition")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    if args.engine == "clang" and not clang_available():
        sys.exit("clang++ not found: install it or use --engine regex")
    if args.engine == "clang":
        hits = scan_clang(args.root)
    elif args.engine == "decl":
        hits = scan_decl(args.root)
    else:
        hits = scan(args.root)
    print_report(hits, args.root, args.verbose, args.engine)

    if args.json:
        # Was `dict(h, hard=True)`, which stamped every finding hard on the way
        # out and quietly threw away whatever the scan had decided. Harmless
        # while every finding really was hard; the moment a soft class appeared
        # it made the report lie.
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([dict(h, hard=bool(h.get("hard", True))) for h in hits],
                      fh, ensure_ascii=False, indent=1)
    return 1 if any(h.get("hard") for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
