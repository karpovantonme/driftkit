#!/usr/bin/env python3
"""gosym.py: cutting a top-level declaration out of Go source.

Needed to compare a copy against its original: not whole files (everything
diverges) but exactly the function or variable that was copied.

Counting braces naively does not work: a brace inside a string, a rune or a
comment throws the depth off and cuts out the wrong block. That is the same
family of mistake as `map<K, V>` with a comma inside angle brackets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------


@dataclass
class Decl:
    kind: str  # func | var | const | type
    name: str
    start: int  # 1-based, line of the declaration
    end: int  # 1-based, inclusive
    text: str

    @property
    def lines(self) -> List[str]:
        return self.text.splitlines()


def strip_code(src: str) -> str:
    """Blank out the contents of strings, runes and comments.

    Length and newlines are preserved, so positions stay the same while braces
    inside literals stop being counted.
    """
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if c == "/" and nxt == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
            continue
        if c == "`":
            out.append(" ")
            i += 1
            while i < n and src[i] != "`":
                out.append("\n" if src[i] == "\n" else " ")
                i += 1
            out.append(" ")
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
            out.append(" ")
            i += 1
            while i < n and src[i] != quote:
                if src[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                if src[i] == "\n":  # unterminated string, stop here
                    break
                out.append(" ")
                i += 1
            out.append(" ")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_DECL = re.compile(
    r"^(?P<kind>func|var|const|type)\s+"
    r"(?:\((?P<recv>[^)]*)\)\s*)?"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b"
)
_GROUP_OPEN = re.compile(r"^(?P<kind>var|const|type)\s*\(\s*$")
_GROUP_MEMBER = re.compile(r"^\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\b")


def _depth_deltas(clean_lines: List[str]) -> List[int]:
    return [
        sum(1 for ch in ln if ch in "({[") - sum(1 for ch in ln if ch in ")}]")
        for ln in clean_lines
    ]


def declarations(src: str) -> List[Decl]:
    """Every top-level declaration, including members of grouped var/const/type."""
    raw_lines = src.splitlines()
    clean_lines = strip_code(src).splitlines()
    while len(clean_lines) < len(raw_lines):
        clean_lines.append("")
    deltas = _depth_deltas(clean_lines)

    out: List[Decl] = []
    i = 0
    n = len(raw_lines)
    while i < n:
        clean = clean_lines[i]
        grp = _GROUP_OPEN.match(clean)
        if grp:
            # grouped declaration: var ( ... ) - members are taken by name
            depth = deltas[i]
            j = i + 1
            while j < n and depth > 0:
                if depth == 1:
                    m = _GROUP_MEMBER.match(clean_lines[j])
                    if m:
                        end = j
                        d = deltas[j]
                        while end + 1 < n and d > 0:
                            end += 1
                            d += deltas[end]
                        out.append(
                            Decl(
                                grp.group("kind"),
                                m.group("name"),
                                j + 1,
                                end + 1,
                                "\n".join(raw_lines[j : end + 1]),
                            )
                        )
                depth += deltas[j]
                j += 1
            i = j
            continue

        m = _DECL.match(clean)
        if m:
            end = i
            depth = deltas[i]
            # single-line declaration: depth never went positive and the line ended
            while end + 1 < n and depth > 0:
                end += 1
                depth += deltas[end]
            out.append(
                Decl(
                    m.group("kind"),
                    m.group("name"),
                    i + 1,
                    end + 1,
                    "\n".join(raw_lines[i : end + 1]),
                )
            )
            i = end + 1
            continue
        i += 1
    return out


def find(src: str, name: str, kind: Optional[str] = None) -> Optional[Decl]:
    """Declaration by exact name. Does not guess between namesakes: returns None."""
    hits = [d for d in declarations(src) if d.name == name and (kind is None or d.kind == kind)]
    if len(hits) != 1:
        return None
    return hits[0]


def next_after(src: str, line: int) -> Optional[Decl]:
    """First declaration starting at or after the given line (1-based).

    This is how a `+lifted:source=...` marker is bound to whatever is written
    below it.
    """
    for d in sorted(declarations(src), key=lambda x: x.start):
        if d.start >= line:
            return d
    return None


# --------------------------------------------------------------------------
# Normalisation for comparing a copy against its original
# --------------------------------------------------------------------------

_QUALIFIER = re.compile(r"\b[a-z][A-Za-z0-9_]*\.(?=[A-Za-z_])")


def norm_line(s: str, drop_qualifiers: bool = False) -> str:
    """A line of code without comments and without indentation differences.

    drop_qualifiers strips `pkg.` before names: package names are almost always
    rewritten when code is copied, and without this everything diverges. The
    stripping is deliberately crude: an extra match makes the tool go quiet
    rather than invent a finding.
    """
    s = strip_code(s + "\n").splitlines()[0] if s else ""
    if drop_qualifiers:
        s = _QUALIFIER.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_body(text: str, drop_qualifiers: bool = False) -> List[str]:
    out = []
    for ln in strip_code(text).splitlines():
        if drop_qualifiers:
            ln = _QUALIFIER.sub("", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            out.append(ln)
    return out


def bodies_equal(a: str, b: str, drop_qualifiers: bool = True) -> bool:
    return norm_body(a, drop_qualifiers) == norm_body(b, drop_qualifiers)


def parse_github_blob(url: str) -> Optional[Tuple[str, str, str, Optional[Tuple[int, int]]]]:
    """Parse a link of the form
    https://github.com/OWNER/REPO/blob/REF/PATH#L10-L20
    into (repo, ref, path, (first, last)).

    Anchor forms seen in the karmada tree: #L10, #L10-L20, #LL266-L276,
    #L563C1-L595, #L6167-L6177, and no anchor at all. All of them must parse.
    """
    m = re.match(r"https://github\.com/([^/\s]+/[^/\s]+)/blob/([^/\s]+)/([^#\s]+)(?:#(\S*))?", url)
    if not m:
        return None
    repo, ref, path, anchor = m.group(1), m.group(2), m.group(3), m.group(4)
    rng = None
    if anchor:
        nums = re.findall(r"L?L?(\d+)(?:C\d+)?", anchor)
        if len(nums) >= 2:
            rng = (int(nums[0]), int(nums[1]))
        elif len(nums) == 1:
            rng = (int(nums[0]), int(nums[0]))
    return repo, ref, path, rng


# --------------------------------------------------------------------------
# Struct fields
# --------------------------------------------------------------------------

_FIELD = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*)\s+[^/\s]")
_EMBEDDED = re.compile(r"^\s+(?:\*?[A-Za-z_][A-Za-z0-9_.]*)\s*(?:`[^`]*`)?\s*$")


def struct_fields(decl: Decl) -> List[Tuple[str, int]]:
    """Field names of a struct and their absolute line numbers.

    Embedded types (`sync.Mutex` with no name) do not count as fields: they have
    no name, so there is nothing to walk. Anything inside nested braces is
    skipped, since those are fields of the nested struct, not this one.
    """
    if decl.kind != "type" or "struct" not in decl.text.split("\n")[0]:
        return []
    raw = decl.text.splitlines()
    clean = strip_code(decl.text).splitlines()
    out: List[Tuple[str, int]] = []
    depth = 0
    for i, ln in enumerate(clean):
        opens = sum(1 for c in ln if c in "{(")
        closes = sum(1 for c in ln if c in ")}")
        if depth == 1 and i > 0:
            if _EMBEDDED.match(ln):
                depth += opens - closes
                continue
            m = _FIELD.match(ln)
            if m:
                for name in m.group(1).split(","):
                    name = name.strip()
                    if name:
                        out.append((name, decl.start + i))
        depth += opens - closes
    return out


# --------------------------------------------------------------------------
# Go-style enumerations: a const block with iota
# --------------------------------------------------------------------------

_IOTA_MEMBER = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s*,\s*[A-Za-z_][A-Za-z0-9_]*)*\s")


def iota_enums(src: str) -> List[Tuple[str, List[Tuple[str, int]], int]]:
    """Enumerations of the form `const ( X Kind = iota; Y; Z )`.

    Returns a list of (type name, [(member, line)], declaration line).
    The type comes from the first line carrying `= iota`: that is the line that
    names it. Without a type the enumeration is skipped, since there would be
    nothing to compare it against.
    """
    raw = src.splitlines()
    clean = strip_code(src).splitlines()
    while len(clean) < len(raw):
        clean.append("")
    out: List[Tuple[str, List[Tuple[str, int]], int]] = []
    i, n = 0, len(clean)
    while i < n:
        if not re.match(r"^const\s*\($", clean[i].strip()):
            i += 1
            continue
        start = i
        depth = clean[i].count("(") - clean[i].count(")")
        members: List[Tuple[str, int]] = []
        type_name = ""
        j = i + 1
        while j < n and depth > 0:
            line = clean[j]
            m = re.search(r"^\s+([A-Za-z_][A-Za-z0-9_]*)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*iota\b", line)
            if m and not type_name:
                type_name = m.group(2).split(".")[-1]
                members.append((m.group(1), j + 1))
            elif type_name:
                mm = _IOTA_MEMBER.match(line + " ")
                if mm and "=" not in line.split("//")[0]:
                    members.append((mm.group(1), j + 1))
                elif mm and re.search(r"=\s*iota", line):
                    members.append((mm.group(1), j + 1))
            depth += line.count("(") - line.count(")")
            j += 1
        if type_name and len(members) >= 3:
            out.append((type_name, members, start + 1))
        i = j
    return out


def switch_cases(body: str) -> Set[str]:
    """Names listed in `case` clauses inside a function body."""
    names: Set[str] = set()
    for m in re.finditer(r"^\s*case\s+([^:]+):", strip_code(body), re.M):
        for part in m.group(1).split(","):
            part = part.strip().split(".")[-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part):
                names.add(part)
    return names
