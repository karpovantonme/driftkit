#!/usr/bin/env python3
"""docdrift.py: numpydoc docstrings against the actual signature.

Two statements about the same thing disagree, and both live in the same
file, usually in the same declaration. That is what makes the fix obvious
to a maintainer and the review short. This is the check that produced the
most merged pull requests in this toolkit.

TWO CLASSES OF FINDING:

  A. **the Parameters section documents an argument the signature does not
     have.** Usually a rename: the code changed, the docstring did not.
     Unambiguous: the signature comes from `ast`, so the list of names is
     exact. Reported as hard.

  B. **the docstring states a default that differs from the real one.**
     Also compared against `ast`, but this compares free TEXT from the
     docstring against a LITERAL from the code, and the text is often prose:
     "default: None or 'auto'", "defaults to the value of ``n_jobs``".
     Reported as soft: a human decides.

The hard/soft split reflects how each check is built, not a measured
confirmation rate. There is no such measurement: findings from this check
have so far been read one by one.

KNOWN BLIND SPOTS:
  - both numpydoc and Google style (`Args:`) are read. Google style was added
    on 19.08.2026 after a run on PySceneDetect reported the project checked
    while looking at 0 of its arguments. reStructuredText fields (`:param x:`)
    are still not parsed;
  - functions with `*args`/`**kwargs` are skipped for class A, since anything
    may legitimately be documented for them;
  - defaults computed by an expression rather than written as a literal are
    skipped: `ast.literal_eval` stays quiet on those, which is correct;
  - test, example and documentation directories are not read: docstrings there
    are written to illustrate, not to describe an API;
  - a directive switching the check off is read in one form only,
    `# numpydoc ignore=`. `# noqa` and `# pylint: disable` are not.

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
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

# The shared skip list plus what is specific to Python projects: docstrings in
# tests, examples and docs are written to illustrate, not to describe an API.
SKIP_DIRS = common.SKIP_DIRS | {
    "test", "tests", "testing", "_test", "doc", "docs", "examples", "benchmarks",
    # `samples` sits next to `examples` for the same reason: adk-python keeps
    # its demo agents in contributing/samples, and a line like
    # `Example: { "location_name": "Basel" }` at parameter indent reads as an
    # argument there.
    "samples", ".tox",
    # Dead code kept in the tree. statsmodels holds a top-level `archive/`, and
    # 12 of the 13 findings there came out of it: textually real, in a
    # directory that is not shipped and that nobody will patch.
    "archive", "_archive", "attic", "legacy", "deprecated", "vendor", "vendored",
    "third_party", "3rdparty",
}

# A trailing colon after the heading is allowed. pyGSTi writes `Returns:` above
# the underline, and without this the word itself became a documented parameter.
# Case does not carry meaning in a section heading. numpydoc capitalises every
# word before comparing, so `See also` is the same heading as `See Also`, and
# networkx writes it that way throughout. Matching case-sensitively meant the
# section never closed and its entries went on being read as parameters.
SEC = re.compile(r'^\s*(Parameters|Other Parameters|Keyword Arguments)\s*:?\s*$', re.I)
# Either character underlines a heading. networkx writes `==========` under
# `Parameters` in nx_latex.py, and with dashes only the whole docstring was
# invisible: no section, no parameters, nothing said about it.
UNDER = re.compile(r'^\s*(-{3,}|={3,})\s*$')
ANYSEC = re.compile(r'^\s*(Returns?|Yields?|Raises|See Also|Notes?|Examples?|References?|Attributes|Warns|Warnings|Methods|Other Parameters)\s*:?\s*$', re.I)
# The type after the colon is optional. Modern numpydoc for annotated code
# writes none, because the type lives in the annotation:
#     cube:
#         Input cube that will be normalized.
# Requiring a type made such a line invisible, and with it every parameter of
# the function. Across the pool that was 2654 functions the tool never looked
# at while reporting their projects as checked: ibis 632 of 682, anndata 36 of
# 39, great-tables 119 of 132.
PARAMLINE = re.compile(
    r'^(?P<ind>\s*)(?P<names>[*\w][\w\s,*]*?)\s*(?::\s*(?P<type>.*?))?\s*$')
# A dot used to terminate the value, so `default 0.01` was read as `0` and
# every fractional default became a mismatch. The value now ends at a sentence
# boundary (". ") or at a comma, not at the first dot. Same family of mistake
# as a flag name truncated mid-word.
# `defaults? to` comes first on purpose. Written the other way round, the
# generic `default[\s:=]+` branch matches "Default to `-1`" as the word
# `default` plus a separator, and the value read out is `to`. Seen in
# keras/src/ops/nn.py.
# The value ends at a sentence boundary, at a semicolon, or at a bracket that
# is not part of the value itself.
#
# 🔴 Two ways this was wrong, both found on live runs 20.08.2026. A comma is a
# thousands separator as often as it is a list separator: `(default: 33,592)`
# was read as `33` and never matched `33_592`. And a subscript carries its own
# brackets: `default=tlist[-1]` was read as `tlist[-1`, which matched nothing
# and printed a finding with a visibly truncated value.
#
# Brackets are now balanced rather than banned, and a comma only ends the value
# when digits do not sit on both sides of it.
DEFAULT_IN_TYPE = re.compile(
    r'(?:defaults?\s+to\s+|default[\s:=]+)(?P<val>.+?)(?=\.\s|;|$)', re.I)


def clean_default(raw: str) -> str:
    """Cut a captured default at the first separator that is really one."""
    out, depth = [], 0
    for i, ch in enumerate(raw):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and ch == ",":
            # A thousands separator has digits on both sides. A list separator
            # does not.
            left = raw[i - 1] if i else ""
            right = raw[i + 1] if i + 1 < len(raw) else ""
            if not (left.isdigit() and right.isdigit()):
                break
        out.append(ch)
    return "".join(out).strip()
EXAMPLES = re.compile(r'^\s*Examples?\s*:?\s*$', re.I)
# Notes inside a Parameters section parse as arguments: the line
# `TODO: looks like not used yet` is shaped exactly like `name : type`.
# Seen in the wild in statsmodels, `gradient_momcond`.
NOTE_WORDS = frozenset({"TODO", "FIXME", "XXX", "HACK", "NOTE", "NB", "BUG", "WARNING"})
# `None` written alone under Parameters is how a docstring says the function
# takes none, and mne uses it. It cannot be an argument name in any case: these
# are keywords, so the rule is exact rather than a guess.
NOT_A_NAME = frozenset({"None", "True", "False"})


def is_note(word: str) -> bool:
    """A placeholder rather than an argument name. pyGSTi writes `todo` in lower case."""
    return word.upper() in NOTE_WORDS or word in NOT_A_NAME


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


def described_below(lines, idx: int, base_ind: int) -> bool:
    """Does a deeper-indented description follow this line."""
    for k in range(idx + 1, min(idx + 3, len(lines))):
        ln = lines[k]
        if not ln.strip():
            continue
        return len(ln) - len(ln.lstrip()) > base_ind
    return False


def block_indent(lines, head: int) -> int:
    """The indent parameter names sit at, which is usually the heading's own.

    Usually, but not always: qutip and graphrag indent the whole block one
    level deeper than the `Parameters` heading above it, and then nothing sits
    at the heading's indent and the function goes unread. The fallback is the
    indent of the FIRST line after the underline, taken by position rather than
    by what it contains. Taking it from the first line that happened to match a
    pattern is the mistake that cost 2654 functions across the pool, so the
    rule stays deterministic.
    """
    base = len(lines[head]) - len(lines[head].lstrip())
    for k in range(head + 2, len(lines)):
        ln = lines[k]
        if not ln.strip():
            continue
        ind = len(ln) - len(ln.lstrip())
        return base if ind <= base else ind
    return base


def section_ends_below(lines, idx: int) -> bool:
    """Is this line the last of the section: only a boundary follows.

    A name written with no type and no description is valid numpydoc, and
    `archive/gam.py` in statsmodels documents `tol` that way on a method that
    does not take it. Requiring a description below made it invisible to us
    while the reference parser saw it, and a divergence between two engines is
    exactly what they exist to surface.
    """
    for k in range(idx + 1, len(lines)):
        ln = lines[k]
        if not ln.strip():
            continue
        return bool(ANYSEC.match(ln) or SEC.match(ln))
    return True


def doc_params(doc):
    """-> [(names, type string, line number inside the docstring)]

    The indentation of a parameter line is known in advance: numpydoc puts
    parameter names at the same indent as the `Parameters` heading and their
    descriptions deeper. Taking the base indent from the first line that
    happened to match instead was the single most expensive bug in this tool.
    A description wrapped onto the next line carries its own colon, and a
    wrapped URL carries one inside `https://`; that line then set the base
    indent, so `https` became a parameter and every real parameter of the
    function became invisible.
    """
    lines = cut_at_examples(doc.split('\n'))
    out, i = [], 0
    while i < len(lines) - 1:
        if SEC.match(lines[i]) and UNDER.match(lines[i + 1]):
            base_ind = block_indent(lines, i)
            j = i + 2
            while j < len(lines):
                ln = lines[j]
                # A heading ends the section when it is underlined, which is
                # how numpydoc writes one, and also when it simply ends in a
                # colon. Mixed docstrings exist: pyvo writes `Returns:` in the
                # Google form inside a numpydoc block, and with only the
                # underlined form recognised the word `Returns` parsed as a
                # parameter of the function.
                if ANYSEC.match(ln) and (
                    (j + 1 < len(lines) and UNDER.match(lines[j + 1]))
                    or ln.rstrip().endswith(":")
                ):
                    break
                m = PARAMLINE.match(ln)
                if m and ln.strip():
                    ind = len(m.group('ind'))
                    # A name written without a colon at all is valid numpydoc
                    # and is what ibis uses throughout: `case_expr` on one line,
                    # the description indented under it. To keep a stray word
                    # from becoming a parameter, such a line counts only when a
                    # deeper-indented description really follows it.
                    if ind == base_ind and m.group('type') is None \
                            and not described_below(lines, j, base_ind) \
                            and not section_ends_below(lines, j):
                        j += 1
                        continue
                    if ind == base_ind:
                        names = [n.strip().lstrip('*') for n in m.group('names').split(',')]
                        names = [n for n in names
                                 if re.fullmatch(r'\w+', n) and not is_note(n)]
                        if names:
                            out.append((names, m.group('type') or '', j))
                j += 1
            i = j
        else:
            i += 1
    return out



# ---------------------------------------------------------------------------
# Google style.
#
# Added 19.08.2026 after a live run on Breakthrough/PySceneDetect: 51 files
# read, `functions with Parameters: 0`, and 0 findings. The tool reported the
# project as checked and had not looked at a single argument, because the
# whole repository writes `Args:` rather than `Parameters` with an underline.
# A throwaway 70-line parser found 3 real mismatches in it.
#
# 🔴 The colon means different things in the two dialects, and reusing PARAMLINE
# here would be silently wrong:
#
#     numpydoc:   name : type          <- after the colon is the TYPE
#     google:     name: description    <- after the colon is the DESCRIPTION
#                 name (type): description
#
# Feeding a description into the type string would make DEFAULT_IN_TYPE read
# "Defaults to 5" out of prose that is not a type, so the type here is taken
# only from the parentheses, and the description is passed along separately so
# a documented default is still found where Google style actually writes it.
GOOGLE_SEC = re.compile(
    r'^\s*(Args|Arguments|Keyword Args|Keyword Arguments|Parameters)\s*:\s*$', re.I)
GOOGLE_ANYSEC = re.compile(
    r'^\s*(Returns?|Yields?|Raises|Note|Notes|Example|Examples|Attributes|'
    r'Todo|Warns|Warning|Warnings|References|See Also|Other Parameters)\s*:\s*$', re.I)
# `name (type, optional): text` and `*args: text` and bare `name: text`.
GOOGLE_PARAM = re.compile(
    r'^(?P<ind>\s*)(?P<name>\*{0,2}\w+)\s*(?:\((?P<type>[^)]*)\))?\s*:\s*(?P<desc>.*)$')


def strip_fences(lines):
    """Drop fenced blocks. What is inside them is an example, not this docstring.

    adk-python shows a prompt template inside triple backticks, and that
    template contains a whole docstring of its own with its own `Args:`. Its
    parameters came out as findings about the enclosing function: timezone,
    location, time. `cut_at_examples` does not help, because a fence needs no
    `Examples:` heading above it.
    """
    out, inside = [], False
    for ln in lines:
        if ln.lstrip().startswith("```"):
            inside = not inside
            out.append("")
            continue
        out.append("" if inside else ln)
    return out


def doc_params_google(doc):
    """-> [(names, type string, line number)], the same shape as doc_params.

    A parameter line sits at one indent and its continuation lines sit deeper,
    which is the only reliable way to tell `name: description` from a wrapped
    sentence that happens to contain a colon. The base indent comes from the
    first parameter under the heading and is then fixed, for the same reason it
    is fixed in the numpydoc parser: letting a later line reset it is how a
    wrapped URL becomes an argument called `https`.
    """
    lines = strip_fences(cut_at_examples(doc.split('\n')))
    out, i = [], 0
    while i < len(lines):
        if not GOOGLE_SEC.match(lines[i]):
            i += 1
            continue
        head_ind = len(lines[i]) - len(lines[i].lstrip())
        j, base_ind = i + 1, None
        while j < len(lines):
            ln = lines[j]
            if not ln.strip():
                j += 1
                continue
            ind = len(ln) - len(ln.lstrip())
            # Back out to the heading level, or a sibling section: done.
            if ind <= head_ind or GOOGLE_ANYSEC.match(ln):
                break
            # A second heading inside the section restarts it rather than
            # counting as a parameter. `application_integration_toolset.py`
            # opens with `"""Args:` as its summary line and carries the real
            # `Args:` below, and the nested heading parsed as an argument
            # literally named Args.
            if GOOGLE_SEC.match(ln):
                base_ind = None
                head_ind = ind
                j += 1
                continue
            m = GOOGLE_PARAM.match(ln)
            if m:
                if base_ind is None:
                    base_ind = ind
                if ind == base_ind:
                    name = m.group('name').lstrip('*')
                    if re.fullmatch(r'\w+', name) and not is_note(name) \
                            and name not in NOT_A_NAME:
                        # The type goes in from the parentheses only. The
                        # description rides along after it so a default written
                        # as "Defaults to 5." is still visible to the caller,
                        # and nothing that is merely prose can pass for a type.
                        typ = (m.group('type') or '').strip()
                        desc = m.group('desc') or ''
                        # Continuation lines join the description. Google style
                        # wraps freely, and keras writes "Default to" at the end
                        # of one line with the value on the next. Reading a
                        # single line then found the words and no value, and the
                        # default came out as the word `to`.
                        k = j + 1
                        while k < len(lines):
                            nxt = lines[k]
                            if not nxt.strip():
                                k += 1
                                continue
                            nxt_ind = len(nxt) - len(nxt.lstrip())
                            if nxt_ind <= base_ind or GOOGLE_ANYSEC.match(nxt):
                                break
                            desc += ' ' + nxt.strip()
                            k += 1
                        out.append(([name], f"{typ} {desc}".strip(), j))
            j += 1
        i = j
    return out


def doc_params_any(doc):
    """numpydoc first, Google style when it finds nothing.

    Order matters rather than being a preference. A numpydoc docstring can
    carry `Returns:` with a colon inside a Notes block and would then look
    faintly like Google style, while the reverse does not happen: Google style
    never underlines a heading. So the stricter dialect is tried first and the
    looser one only answers where the strict one saw nothing at all.
    """
    return doc_params(doc) or doc_params_google(doc)


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


# --------------------------------------------------------------------------
# Places the author marked as excluded from checking
# --------------------------------------------------------------------------
#
# numpydoc lets an author switch a check off where it does not apply, by a
# comment inside the signature:
#
#     def apply_where(  # type: ignore[explicit-any] # numpydoc ignore=PR01,PR02
#         cond, args, f1, f2=None, /, *, fill_value=None
#     ):
#
# `PR02` is "unknown parameters", which is class A word for word. In
# statsmodels the documented `xp` is deliberate and the author said so in the
# source; reporting it means telling a maintainer something they already
# decided. The directive is read exactly: only a code list containing PR02, or
# a directive with no codes at all, silences class A. `EX01` and its kind are
# about other checks and leave this one judging.
SUPPRESS = re.compile(
    r"#\s*numpydoc\s+ignore\s*(?:=\s*(?P<codes>[A-Z]{2}\d{2}(?:\s*,\s*[A-Z]{2}\d{2})*))?")
# The class A code and the blanket ones, which cover every check at once.
SUPPRESS_CODES = frozenset({"PR02", "GL08"})


def author_switched_off(lines: List[str], fn) -> bool:
    """Did the author disable the parameter check on this function.

    The directive lives anywhere in the signature, from `def` to the line above
    the docstring, because that is where numpydoc itself reads it from.
    """
    first = fn.lineno - 1
    last = fn.body[0].lineno - 1 if fn.body else first + 1
    for ln in lines[first:max(last, first + 1)]:
        m = SUPPRESS.search(ln)
        if not m:
            continue
        codes = m.group("codes")
        if not codes:
            return True
        if {c.strip() for c in codes.split(",")} & SUPPRESS_CODES:
            return True
    return False


def is_property(fn) -> bool:
    """Is this a property (property, cached_property, functools.cached_property)."""
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        name = getattr(node, "attr", None) or getattr(node, "id", "") or ""
        if "property" in str(name):
            return True
    return False


def replaced_in_body(fn, name: str) -> bool:
    """Does the body immediately swap this argument for something else.

    A sentinel default is a real idiom and the docstring that names the real
    value is telling the truth. qutip writes `T: float = 0.0` and then
    `T = T or tlist[-1]` on the first line, and documents `default=tlist[-1]`.
    Comparing against the signature alone calls that a mismatch, and it is not.

    Only the opening statements count, but the search goes inside them: qutip
    puts `T = T or tlist[-1]` in the else branch of the first `if`, and looking
    at the top level alone missed it. Reading the whole body instead would
    silence real findings, since reassigning an argument halfway down is
    ordinary flow rather than a default.
    """
    opening = []
    for st in fn.body[:4]:
        opening.extend(ast.walk(st))
    for node in opening:
        # `x = x or something`
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            tgt = node.targets[0]
            if isinstance(tgt, ast.Name) and tgt.id == name:
                v = node.value
                if isinstance(v, ast.BoolOp) and isinstance(v.op, ast.Or):
                    if any(isinstance(x, ast.Name) and x.id == name for x in v.values):
                        return True
                if isinstance(v, ast.IfExp):
                    return True
        # `if x is None: x = something`
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) \
                    and test.left.id == name \
                    and any(isinstance(o, (ast.Is, ast.Eq)) for o in test.ops):
                for inner in node.body:
                    if isinstance(inner, ast.Assign) and len(inner.targets) == 1:
                        it = inner.targets[0]
                        if isinstance(it, ast.Name) and it.id == name:
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
    """Does the documented default read as prose.

    Docstrings routinely state meaning instead of a literal: "default: all
    nodes in G", "default: len(G)", "default: first node in list(G)".
    Comparing that against a literal from the code is pointless.
    """
    v = val.strip()
    return bool(not v or " " in v or v.count("(") != v.count(")"))


def norm(v):
    """Strip the punctuation a docstring wraps a value in, until it stops changing.

    One pass is not enough and the reason is worth keeping. A docstring writes
    "Defaults to `-1`." and the two kinds of rubbish hide behind each other:
    stripping quotes first leaves the trailing backtick under the full stop,
    and stripping the stop afterwards has nothing left to run. The value came
    out as `-1` with a backtick still attached and matched no literal at all.

    Measured 20.08.2026 on keras and flax: 40 of 57 false positives in one run
    were this and nothing else. Same shape as the quote hiding under a bracket
    in linkdrift, fixed there the day before.
    """
    s = str(v).strip()
    while True:
        before = s
        s = s.strip('`"\'').strip().rstrip('.').strip()
        if s == before:
            break
    # A thousands separator is presentation, not value. The docstring writes
    # 33,592 and the code writes 33_592; both mean the same number and the
    # comparison has to see that. Python's own separator goes too, since the
    # literal side arrives as text here.
    if re.fullmatch(r'-?\d{1,3}(?:[,_]\d{3})+(?:\.\d+)?', s):
        s = s.replace(',', '').replace('_', '')
    return {'true': 'True', 'false': 'False', 'none': 'None'}.get(s.lower(), s)


def scan_file(path, repo):
    """Findings in one file. A file that fails to parse is counted apart.

    Swallowing a `SyntaxError` and counting the file as read makes the report
    look **cleaner** rather than more worrying. Three files of ESMValCore use
    syntax newer than the interpreter the run happened on; twelve functions
    disappeared and the coverage line did not move. Same mechanism as the
    `.github` directory dropped by a hidden-name mask.
    """
    try:
        src = path.read_text(encoding='utf-8', errors='ignore')
        tree = ast.parse(src)
    except SyntaxError:
        COUNTS["unparsable"] += 1
        return []
    except Exception:
        COUNTS["unreadable"] += 1
        return []
    lines = src.split("\n")
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
        if author_switched_off(lines, fn):
            COUNTS["suppressed"] += 1
            continue
        dp = doc_params_any(doc)
        if not dp:
            # A docstring is here and no parameters came out of it. Either it
            # documents none, or it is written in a dialect this file cannot
            # read. Counting the difference is the only thing standing between
            # a clean report and a blind one: on deepinv the tool read 14
            # functions out of 1232 documented, found nothing, and printed a
            # coverage block that looked healthy. 1204 of those are Sphinx
            # `:param x:` fields, a dialect not parsed here at all.
            if DOCUMENTS_SOMETHING.search(doc):
                COUNTS["unread_docstrings"] += 1
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
                        if replaced_in_body(fn, dn):
                            # The signature holds a sentinel and the body swaps
                            # it on the first line. The docstring names what it
                            # becomes, and that is the honest thing to document.
                            continue
                        want = norm(clean_default(m.group('val')))
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


# A docstring that names arguments in some dialect. Used only to tell "this
# function documents nothing" apart from "this file cannot read how it does",
# which is the difference between an honest zero and a silent one.
#
# 🔴 The Sphinx form puts the type BEFORE the name: `:param int in_channels:`.
# A pattern demanding one word before the colon counted 88 unread docstrings on
# deepinv where the tree holds 4697 `:param` fields, so the warning it printed
# was itself an understatement.
DOCUMENTS_SOMETHING = re.compile(
    r'^\s*(?:'
    r':(?:param|parameter|arg|argument|key|keyword|type)\s+[^:]+:'   # Sphinx / reST
    r'|@(?:param|keyword|type)\s+\w+'                               # Epytext, old Epydoc
    r'|\\param\b|@param\b'                                          # Doxygen, in C++ bindings
    r'|Args?\s*:|Arguments\s*:|Keyword Args?\s*:'                   # Google
    r'|Params?\s*:'                                                  # anndata writes `Params:`
    r'|Parameters\s*:?\s*$'                                         # numpydoc heading
    r')', re.M | re.I)

COUNTS: Dict[str, int] = {"files": 0, "funcs": 0, "props": 0, "compat_blind": 0,
                          "unread_docstrings": 0,
                          "unparsable": 0, "unreadable": 0, "numpydoc_failed": 0,
                          "suppressed": 0}


REQUIRES = re.compile(r'requires-python\s*=\s*["\']([^"\']+)', re.I)


def interpreter_gap(root: str) -> str:
    """What the project asks for against what is running this scan.

    A file whose syntax is newer than the interpreter does not parse, and the
    functions inside it are simply never looked at. On anndata that was 21 files
    of 39 functions, and the tool used to say nothing at all. Saying it before
    the run costs one line and saves the whole class.
    """
    for name in ("pyproject.toml", "setup.cfg"):
        text = common.read_text(os.path.join(root, name))
        m = REQUIRES.search(text or "")
        if not m:
            continue
        v = re.search(r"(\d+)\.(\d+)", m.group(1))
        if not v:
            continue
        want = (int(v.group(1)), int(v.group(2)))
        have = sys.version_info[:2]
        if want > have:
            return (f"project asks for Python {want[0]}.{want[1]}, "
                    f"this run is {have[0]}.{have[1]}")
    return ""


def scan(root: str) -> List[dict]:
    """Walk the tree. Test files are skipped by name, not only by directory."""
    COUNTS.update(files=0, funcs=0, props=0, compat_blind=0, unparsable=0,
                  unreadable=0, suppressed=0)
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
        hits.extend(scan_file(p, name))
        COUNTS["files"] += 1
    return hits


# --------------------------------------------------------------------------
# Second engine: the reference parser of the format itself
# --------------------------------------------------------------------------


def numpydoc_available() -> bool:
    try:
        import numpydoc.docscrape  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def split_names(head: str) -> List[str]:
    """`a, b, c` is three names; a sentence that happens to contain commas is none.

    The reference parser puts a whole line into the name whenever the line does
    not look like a declaration, and a Parameters section often holds prose:
    "Must contain ECoG, sEEG or DBS channels", a citation list, "int, required".
    Splitting that on commas produced names like `ECoG`, `Gilbert` and
    `required`. A comma-separated list is only taken when EVERY piece of it is
    a bare identifier, which is what a real one always is.
    """
    parts = [n.strip().lstrip("*") for n in head.split(",")]
    if not all(re.fullmatch(r"\w+", n) for n in parts):
        return []
    return [n for n in parts if not is_note(n)]


def doc_params_numpydoc(doc: str):
    """The same list, parsed by numpydoc rather than by the rules above.

    numpydoc is the reference implementation of the format, so it is the
    natural thing to check our own parser against. What it is NOT used for is
    validation: `numpydoc.validate` imports the module it inspects, and running
    imports across foreign clones means executing their code. Only the parser
    is used here, and the signature still comes from `ast`.

    Reading its output takes care. With no space before the colon, as in
    `create_using: NetworkX graph container, optional`, numpydoc puts the whole
    line into the name and leaves the type empty. Splitting that on commas turns
    the word `optional` into a parameter: 291 of them across the pool, and every
    one an artefact of the adapter rather than of either parser. So the name is
    cut at the first colon, and only then split.
    """
    from numpydoc.docscrape import NumpyDocString

    try:
        parsed = NumpyDocString(doc)
    except Exception:  # noqa: BLE001
        COUNTS["numpydoc_failed"] += 1
        return []
    out = []
    for section in ("Parameters", "Other Parameters"):
        for p in parsed.get(section, []):
            head = p.name.split(":", 1)[0]
            names = split_names(head)
            if names:
                out.append((names, p.type or "", 0))
    return out


def scan_file_numpydoc(path, repo):
    """Class A only: the reference parser gives no line for a parameter."""
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)
    except SyntaxError:
        COUNTS["unparsable"] += 1
        return []
    except Exception:  # noqa: BLE001
        COUNTS["unreadable"] += 1
        return []
    lines = src.split("\n")
    res = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(fn, clean=False)
        if not doc:
            continue
        dp = doc_params_numpydoc(doc)
        if not dp:
            continue
        COUNTS["funcs"] += 1
        if is_property(fn):
            COUNTS["props"] += 1
            continue
        if author_switched_off(lines, fn):
            COUNTS["suppressed"] += 1
            continue
        names, _defaults, star = sig_params(fn)
        extra, blind = compat_names(fn)
        if blind:
            COUNTS["compat_blind"] += 1
            continue
        nameset = set(names) | extra | {"self", "cls", "kwargs", "args"}
        for docnames, typ, _ln in dp:
            for dn in docnames:
                if dn not in nameset and not star:
                    res.append(dict(kind="A", repo=repo, file=str(path), line=fn.lineno,
                                    func=fn.name, param=dn, sig=names,
                                    type=(typ or "")[:70], engine="numpydoc"))
    return res


def scan_numpydoc(root: str) -> List[dict]:
    COUNTS.update(files=0, funcs=0, props=0, compat_blind=0, unparsable=0,
                  unreadable=0, numpydoc_failed=0, suppressed=0)
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
        hits.extend(scan_file_numpydoc(p, name))
        COUNTS["files"] += 1
    return hits


def is_hard(hit: dict) -> bool:
    """Hard means class A only: a documented name absent from the signature."""
    return hit.get("kind") == "A"



# ---------------------------------------------------------------------------
# Severity, added 20.08.2026.
#
# A finding whose parameter guards something costs more when it is wrong. In
# keras, `deserialize_keras_object` documented `safe_mode` as defaulting to
# False when it defaults to True: the text says the protection against unsafe
# lambda deserialization is off, and explains how to turn it off further. A
# misspelled parameter name costs a reader a minute. That line invites them to
# disable arbitrary code execution protection they already had.
#
# 🔴 This is a lens on findings already made, not a search of its own, and the
# distinction is the whole point. A sweep over 59 repositories produced 607
# findings, 3 carried a guard-shaped name and 2 of those were real: 0.49%.
# Four separate probes of what docdrift structurally cannot see returned zero
# security findings between them, because the pools barely overlap. Guard flags
# live where reST and Google style are written; the numpydoc blind spot lives
# in the scientific stack, which has almost no guard flags.
#
# So there is no securedrift to write. There is a word list and a flag, and
# their value is triage: which of 607 to send first, and in what words.
#
# 🔴 What this is not: a vulnerability scanner. It reports that a sentence and
# a signature disagree about something protective. It says nothing about
# whether anything is exploitable, and a report built on it must not imply
# otherwise. Real security findings have their own etiquette, private
# disclosure and windows, and walking into that without grounds is how you get
# ignored at best.
GUARD_WORDS = frozenset({
    "safe", "safe_mode", "unsafe", "secure", "insecure", "verify", "verified",
    "verification", "validate", "validation", "strict", "check", "checks",
    "trust", "trusted", "untrusted", "sanitize", "sanitise", "escape",
    "allow_unsafe", "check_hostname", "ssl", "tls", "cert", "certificate",
    "auth", "authenticate", "authorization", "permission", "privilege",
    "sandbox", "isolation", "signature_check", "weights_only",
})


def guards_something(param: str) -> bool:
    """True when the parameter name reads as a guard.

    Split on the usual separators rather than matched as a substring: `check`
    must fire on `check_hostname` and stay quiet on `checkpoint`, which is
    ordinary in this corpus and guards nothing.
    """
    parts = re.split(r"[_\-.]", param.lower())
    return any(p in GUARD_WORDS for p in parts) or param.lower() in GUARD_WORDS


def print_report(hits: List[dict], root: str, verbose: bool = False,
                 engine: str = "rules") -> None:
    for h in hits:
        h["guards"] = guards_something(h.get("param", ""))
    hard = [h for h in hits if is_hard(h)]
    soft = [h for h in hits if not is_hard(h)]
    guarded = [h for h in hits if h["guards"]]

    if hard:
        print(f"\n=== Documented name not in the signature ({len(hard)}) ===")
        for h in hard:
            mark = "  [guard]" if h["guards"] else ""
            print(f"\n  {h['file']}:{h['line']}  {h['func']}(){mark}")
            print(f"    docstring:  {h['param']}")
            print(f"    code:       {', '.join(h['sig'][:8])}")
    if soft:
        print(f"\n=== Documented default differs ({len(soft)}) ===")
        for h in soft[: (len(soft) if verbose else 30)]:
            mark = "  [guard]" if h["guards"] else ""
            print(f"\n  {h['file']}:{h['line']}  {h['func']}()  {h['param']}{mark}")
            print(f"    docstring:  {h['doc_default']}")
            print(f"    code:       {h['real_default']}")
        if not verbose and len(soft) > 30:
            print(f"\n  ... {len(soft) - 30} more, use -v for the full list")

    print("\n=== Coverage ===")
    print(f"  tree:                   {root}")
    gap = interpreter_gap(root) if os.path.isdir(root) else ""
    read_ok = COUNTS['files'] - COUNTS['unparsable'] - COUNTS['unreadable']
    print(f"  engine:                 {engine}")
    print(f"  files read:             {read_ok}")
    # Printed even at zero: a silent parse failure is how a report gets
    # cleaner instead of more worrying.
    print(f"  files that failed to parse: {COUNTS['unparsable']}"
          f" (syntax newer than the running interpreter)")
    if COUNTS['unreadable']:
        print(f"  files unreadable:       {COUNTS['unreadable']}")
    if gap:
        print(f"  interpreter is older:   {gap}")
    print(f"  functions with Parameters: {COUNTS['funcs']}")
    unread = COUNTS.get("unread_docstrings", 0)
    if unread:
        documented = COUNTS['funcs'] + unread
        share = 100 * COUNTS['funcs'] / documented
        mark = "🔴 " if share < 50 else ""
        print(f"  {mark}docstrings not read:    {unread} of {documented} documented"
              f" ({share:.0f}% parsed). A dialect this file does not know,"
              f" most often Sphinx `:param x:`")
    print(f"  properties skipped:     {COUNTS['props']} (docstring describes what they return)")
    # Printed even at zero, like every other line here: a dismissal nobody sees
    # is how a report gets cleaner instead of more honest.
    print(f"  author switched it off: {COUNTS['suppressed']} (`# numpydoc ignore=PR02` in the signature)")
    if COUNTS["compat_blind"]:
        print(f"  opaque decorator:       {COUNTS['compat_blind']} (cannot tell which extra names it accepts)")
    if guarded:
        print(f"  🔴 about a guard:         {len(guarded)}"
              f" (the name reads as protective, so a wrong line here costs more)")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="numpydoc docstrings against the signature")
    ap.add_argument("root", help="project directory")
    ap.add_argument("name", nargs="?", help="project name for the report")
    ap.add_argument("--engine", choices=("rules", "numpydoc"), default="rules",
                    help="rules is the parser written here, numpydoc is the reference one")
    common.add_common_args(ap)
    args = ap.parse_args(argv)

    if args.engine == "numpydoc" and not numpydoc_available():
        sys.exit("numpydoc is not installed: pip install numpydoc, or use --engine rules")
    hits = scan_numpydoc(args.root) if args.engine == "numpydoc" else scan(args.root)
    print_report(hits, args.name or args.root, args.verbose, args.engine)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [dict(h, hard=is_hard(h)) for h in hits],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(is_hard(h) for h in hits) else 0


if __name__ == "__main__":
    sys.exit(main())
