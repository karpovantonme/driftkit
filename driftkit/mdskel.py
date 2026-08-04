#!/usr/bin/env python3
"""mdskel.py: the skeleton of a markdown page.

Prose gets translated, the skeleton does not. Code blocks, external links,
image paths and heading levels stay the same in any language, so a translation
can be compared against its original through them without knowing the language.

A separate module because the skeleton is needed by `transdrift.py` and by any
future documentation tool.

The parse is line by line and deliberately boring. The one place it is easy to
lie is forgetting that what sits inside ``` is text, not markup: a `# comment`
line in a Python block is not a heading. Same family of mistake as a brace
inside a string literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------


@dataclass
class Skeleton:
    front_matter: Dict[str, str] = dc_field(default_factory=dict)
    headings: List[Tuple[int, int]] = dc_field(default_factory=list)  # (level, line)
    code_blocks: List[Tuple[str, str, int]] = dc_field(default_factory=list)  # (language, body, line)
    links: List[Tuple[str, int]] = dc_field(default_factory=list)  # (target, line)
    images: List[Tuple[str, int]] = dc_field(default_factory=list)
    body_lines: int = 0

    def size(self) -> int:
        return len(self.headings) + len(self.code_blocks) + len(self.links) + len(self.images)


_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([A-Za-z0-9_+-]*)")
_HEADING = re.compile(r"^(#{1,6})\s+\S")
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_HTML_SRC = re.compile(r"<img[^>]+src=[\"']([^\"']+)")
_FM_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")


def parse(text: str) -> Skeleton:
    lines = text.splitlines()
    sk = Skeleton()
    i = 0
    n = len(lines)

    # front matter between --- and ---
    if n and lines[0].strip() == "---":
        j = 1
        while j < n and lines[j].strip() != "---":
            m = _FM_KEY.match(lines[j])
            if m:
                sk.front_matter[m.group(1)] = m.group(2).strip().strip("\"'")
            j += 1
        i = j + 1

    body_start = i
    fence: Optional[str] = None
    fence_lang = ""
    fence_start = 0
    buf: List[str] = []

    while i < n:
        ln = lines[i]
        m = _FENCE.match(ln)
        if fence is None and m:
            fence = m.group(2)[0] * 3
            fence_lang = m.group(3)
            fence_start = i + 1
            buf = []
            i += 1
            continue
        if fence is not None:
            # closing fence: same marker, no language after it
            if m and m.group(2)[0] * 3 == fence and not m.group(3):
                sk.code_blocks.append((fence_lang, "\n".join(buf), fence_start))
                fence = None
                i += 1
                continue
            buf.append(ln)
            i += 1
            continue

        # everything below is only read outside a code block
        h = _HEADING.match(ln)
        if h:
            sk.headings.append((len(h.group(1)), i + 1))
        for mm in _IMAGE.finditer(ln):
            sk.images.append((mm.group(1), i + 1))
        for mm in _HTML_SRC.finditer(ln):
            sk.images.append((mm.group(1), i + 1))
        for mm in _LINK.finditer(ln):
            target = mm.group(1)
            if not ln[max(0, mm.start() - 1) : mm.start()] == "!":
                sk.links.append((target, i + 1))
        i += 1

    if fence is not None:  # unterminated fence: read to the end of the file
        sk.code_blocks.append((fence_lang, "\n".join(buf), fence_start))

    sk.body_lines = max(0, n - body_start)
    return sk


# --------------------------------------------------------------------------
# Normalisation for comparing a translation against its original
# --------------------------------------------------------------------------

# The space after the marker is optional: `//Make sure` is everywhere, and with
# a mandatory space such a line was passing as code.
_COMMENT_LINE = re.compile(r"^\s*(?:#|//|--|;|<!--|/\*|\*)")

# Block languages whose content is meant to be translated: diagram node labels
# are prose, not code. Comparing them is pointless.
PROSE_LANGS = frozenset({"mermaid", "plantuml", "text", "txt", "plaintext", "ascii"})

# A trailing comment on a line of code gets translated too:
#   'instrumentation-scope-name', //name (required)
#   'instrumentation-scope-name', // 名前（必須）
# The space after the marker is optional: originals often omit it, translations
# almost always add it, and without this the whole block reads as missing.
# The colon guard before `//` protects URLs such as https://..., which must not
# be cut. `--` and `;` are deliberately NOT in the set: `docker run --rm` is not
# a comment.
_TRAILING_COMMENT = re.compile(r"(?<!:)\s+(?://|#).*$")
_LOCALE_PREFIX = re.compile(r"^/(?:[a-z]{2}(?:[-_][A-Za-z]{2})?)/")


def norm_code(body: str) -> str:
    """A code block body without comments and without indentation differences.

    Comments inside examples do get translated: Kubernetes says so explicitly in
    its guidelines. Comparing blocks together with them means declaring the very
    act of translation a mismatch.
    """
    out = []
    for ln in body.splitlines():
        if _COMMENT_LINE.match(ln):
            continue
        ln = _TRAILING_COMMENT.sub("", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            out.append(ln)
    return "\n".join(out)


def norm_link(target: str) -> str:
    """A link target without its locale prefix and without the anchor.

    A translation honestly points at its own locale: `/ja/docs/x` against
    `/docs/x`. Treating that as a mismatch means drowning in noise.
    """
    t = target.strip()
    t = t.split("#", 1)[0]
    if t.startswith(("http://", "https://")):
        return t.rstrip("/")
    t = _LOCALE_PREFIX.sub("/", t)
    return t.rstrip("/") or "/"


def is_external(target: str) -> bool:
    return target.startswith(("http://", "https://"))
