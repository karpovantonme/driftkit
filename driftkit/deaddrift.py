#!/usr/bin/env python3
"""deaddrift.py: removed, and still promised.

The second largest species by volume: seven submissions, two merges, and until
now it was done by hand. That is how etcd was found (six proxy settings cut in
3.6 still living in the sample configuration) and rclone (the `--dump-auth` flag
removed when `--dump` arrived).

The method: take the changelog, find the sections about removals, write out the
flag and setting names from them, then look for those names in the documentation
and sample configuration. It works because a project's own checks compare the
**generated** help blocks and never cover free prose.

WHAT THE TOOL DOES NOT CALL A FINDING. The list comes from dry holes already
checked by hand, so nobody repeats that work:

  - **a mention honestly qualified by version.** In Prometheus every mention of a
    removed flag carries "for Prometheus versions v2.38 and below". That is
    correct documentation. The signal is a version number or the words
    before / prior / legacy / no longer nearby;
  - **older documentation versions** (`docs/v3.5/`, `versioned_docs/`,
    `content/en/v1.2/`): removed things are required to stay there;
  - **the changelog itself** and its neighbours: writing about this is their job;
  - **a name still defined in the code**: then something else was removed, or the
    removal was partial.

Run:
  python3 deaddrift.py --dir ~/Projects/oss/etcd
  python3 deaddrift.py --dir ... -v      # list what was dismissed, by name

Tests: test_deaddrift.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Set, Tuple

SKIP_DIRS = common.SKIP_DIRS  # one list for the whole kit

CHANGELOG_NAME = re.compile(r"^(CHANGELOG|CHANGES|NEWS|RELEASES?|HISTORY)", re.I)
DOC_EXT = (".md", ".rst", ".txt", ".adoc")
CONFIG_EXT = (".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".json", ".env", ".properties")
CODE_EXT = (".go", ".c", ".h", ".cc", ".cpp", ".rs", ".py", ".java", ".js", ".ts", ".rb", ".sh")

# Sample-configuration suffixes: in etcd that is `etcd.conf.yml.sample`, exactly
# the file where the removed proxy settings lived. Without stripping the suffix
# it was never read.
SAMPLE_SUFFIX = (".sample", ".example", ".template", ".dist", ".tpl", ".in", ".orig")


def real_ext(name: str) -> str:
    base = name
    for suf in SAMPLE_SUFFIX:
        if base.lower().endswith(suf):
            base = base[: -len(suf)]
            break
    return os.path.splitext(base)[1].lower()

# A section heading about removals.
# Only sections about removal proper. `Breaking Changes` is deliberately NOT
# here: such a section lists additions, renames and default changes as well. On
# etcd it produced all four false findings out of four.
# `Deprecated` is deliberately NOT here either: a deprecated option still works
# and documenting it is correct. On nf-core the Deprecated sections gave 27 false
# findings out of 81 on sarek alone.
REMOVED_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s*.*\b(removed?|removals?)\b", re.I
)

# "X is now Y", "X renamed to Y", "X replaced by Y": X is gone while Y is alive
# and documented by right. Without this rule a live option gets declared removed:
# in sarek that happened to `--skip_tools`, which appears in the documentation 8
# times and entirely legitimately.
RENAME_MARKER = re.compile(
    r"\b(?:is\s+now|are\s+now|renamed\s+(?:to|into)|replaced\s+(?:by|with)|"
    r"use\s+(?:\S+\s+)?instead|instead\s+of)\b|->", re.I
)

# A line like "Restart from `--step annotate` from folder is removed": what went
# away is one mode rather than the option. The option name here is part of a
# scenario description. On sarek those gave 30 false out of 30 after the first two
# fixes: `--step` and `--genome` are live pipeline parameters.
SCOPED_REMOVAL = re.compile(
    r"\b(?:restart|support|possibility|option\s+to|ability|use\s+of)\b", re.I
)
ANY_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s")
# A standalone bullet about a removal
REMOVED_BULLET = re.compile(r"^\s*[-*]\s*(?:the\s+)?(?:following\s+)?\b(removed?|deleted?|dropped?)\b", re.I)

# The name has to sit NEXT TO the removal verb rather than anywhere in the
# sentence. "Removed `--dump-auth`" passes, "Remove spurious error message on\n# `--sftp-disable-concurrent-reads`" does not: what went away is the message.\n# The window used to be 40 characters and let the second form through. On rclone
# that single line produced all nine "findings" out of nine.
BULLET_NAME_WINDOW = 15
# The reverse form ("`--foo` has been removed") is deliberately absent. It was
# written speculatively rather than derived from a known case, and it produced
# 14,022 false findings on karmada: almost any line where a name sits near the
# word removed matches it. Both proven cases, etcd and rclone, are caught by a
# section heading and a "Removed X" bullet, which is enough.

# Names we know how to extract
NAME_PATTERNS = (
    # Single-segment flags are required: in the known etcd case the removed flag
    # was exactly `--proxy`, and with two segments required it was never found.
    # Capital letters are required: without them `--genomeDict` truncates to
    # `--genome`, a live sarek parameter. The third case of one family, where a
    # name truncated by the parser matches a real one and becomes a false finding
    # (the earlier two were `--sftp-disab` and the 40-character window).
    # The dot is required: thanos has flags like `--request.logging-config`, and
    # without it the name truncates to `--request`. The fourth case of the family.
    re.compile(r"(?<![\w.-])--[A-Za-z][A-Za-z0-9]{2,}(?:[-_.][A-Za-z0-9]+)*"),  # long flag
    re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}\b"),           # ENVIRONMENT_VARIABLE
)

# Signs of an honest caveat: the mention refers to the past
QUALIFIED = re.compile(
    r"\b(?:v?\d+\.\d+|before|prior\s+to|until|up\s+to|older|legacy|no\s+longer|"
    r"removed|deprecated|obsolete|was\s+removed|earlier|previous|historical|"
    r"was removed|has been removed)\b",
    re.I,
)

# Directories and files where removed things are required to stay: archives,
# older documentation versions, postmortems, the README of a previous major.
OLD_DOCS = re.compile(
    r"(?:^|/)(?:versioned_docs|v\d+(?:\.\d+)*|_archive|archive|history|"
    r"postmortems?|deprecated|legacy|old)(?:/|$)"
    r"|(?:^|/)[A-Za-z_-]*v\d+\.[a-z]+$"
    r"|(?:^|/)[A-Za-z_-]*(?:postmortem|migration|upgrade|history)[A-Za-z_-]*\.[a-z]+$",
    re.I,
)


@dataclass
class Finding:
    name: str
    hard: bool
    where: str
    changelog_ref: str
    section: str
    line_text: str
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    changelogs: List[str] = dc_field(default_factory=list)
    sections: int = 0
    removed_names: Dict[str, Tuple[str, str]] = dc_field(default_factory=dict)  # name -> (coordinate, section)
    files_searched: int = 0
    far_from_verb: List[str] = dc_field(default_factory=list)
    survivors: Set[str] = dc_field(default_factory=set)
    renamed_away: List[str] = dc_field(default_factory=list)
    scoped: List[str] = dc_field(default_factory=list)
    resurrected: List[str] = dc_field(default_factory=list)
    truncated: List[str] = dc_field(default_factory=list)
    qualified: List[str] = dc_field(default_factory=list)
    old_docs: List[str] = dc_field(default_factory=list)
    still_in_code: List[str] = dc_field(default_factory=list)
    findings: List[Finding] = dc_field(default_factory=list)


# --------------------------------------------------------------------------


def find_changelogs(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        base = os.path.basename(dirpath)
        for n in sorted(names):
            if not n.endswith(DOC_EXT):
                continue
            if CHANGELOG_NAME.match(n) or CHANGELOG_NAME.match(base):
                out.append(os.path.join(dirpath, n))
    return out


def extract_removed(path: str, root: str, report: Report) -> None:
    """Names from removal sections and bullets."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    rel = os.path.relpath(path, root)

    in_section = False
    section_level = 0
    section_title = ""
    for i, line in enumerate(lines, 1):
        h = ANY_HEADING.match(line)
        if h:
            level = len(h.group(1))
            if REMOVED_HEADING.match(line):
                in_section, section_level, section_title = True, level, line.strip("# ").strip()
                report.sections += 1
                continue
            if in_section and level <= section_level:
                in_section = False

        if in_section:
            # On a rename line take only what is LEFT of the marker: the
            # successor name sits on the right and is alive.
            scan = line
            if SCOPED_REMOVAL.search(line):
                report.scoped.append(f"{rel}:{i}: a scenario went away rather than the name: {line.strip()[:64]}")
                continue
            mark = RENAME_MARKER.search(line)
            if mark:
                scan = line[: mark.start()]
                for rx in NAME_PATTERNS:
                    for m in rx.finditer(line[mark.end():]):
                        report.survivors.add(m.group(0))
            for rx in NAME_PATTERNS:
                for m in rx.finditer(scan):
                    if not not_truncated(m.group(0), line):
                        report.truncated.append(f"{rel}:{i}: the name {m.group(0)} was truncated by the parser")
                        continue
                    report.removed_names.setdefault(m.group(0), (f"{rel}:{i}", section_title))
            continue

        bullet = REMOVED_BULLET.match(line)
        if bullet:
            # Search the FULL line and check the window by the start position of
            # the name. Slicing the line would cut a name in half, and the stump
            # `--sftp-disab` would then match as a substring anywhere.
            limit = bullet.end() + BULLET_NAME_WINDOW
            for rx in NAME_PATTERNS:
                m = rx.search(line, bullet.end())
                if m and m.start() <= limit:
                    report.removed_names.setdefault(
                        m.group(0), (f"{rel}:{i}", line.strip()[:80])
                    )
                    break
            else:
                report.far_from_verb.append(f"{rel}:{i} - {line.strip()[:70]}")
            continue


def not_truncated(name: str, line: str) -> bool:
    """Whether the name was truncated by the parser.

    A general rule against a whole family of mistakes. Four times in a row the
    same thing: `--sftp-disab` (the window sliced the line), `--genome` out of
    `--genomeDict` (a pattern without capitals), `--request` out of
    `--request.logging-config` (a pattern without the dot). Every time the stump
    matched a real name and produced false findings by the handful.

    The check does not depend on which character was forgotten: if the name
    continues on the same line, the parse stopped halfway.
    """
    for m in re.finditer(re.escape(name), line):
        tail = line[m.end() : m.end() + 1]
        if tail and (tail.isalnum() or tail in "-_."):
            continue
        return True
    return False


def _whole(name: str) -> re.Pattern:
    """A whole name rather than a substring: `--dump` must not match
    `--dump-headers`, otherwise one finding multiplies across the docs."""
    if name.startswith("--"):
        return re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])")
    return re.compile(r"(?<![\w])" + re.escape(name) + r"(?![\w])")


def drop_resurrected(report: Report) -> None:
    """A name mentioned ABOVE the removal line in a changelog is alive.

    A changelog runs top down from newer versions to older ones, so a mention
    above is a later one. When a name resurfaces after it was removed, either it
    came back or something else was removed.

    The live case: the sarek changelog goes back to version 0.1 and lists `--step`
    as removed in a 2019 entry ("`--step` in `annotate.nf`, `germlineVC.nf` and
    `somatic.nf`"). Today it is a live pipeline parameter appearing in the schema
    and in the documentation 18 times.
    """
    for name, (ref, _sec) in list(report.removed_names.items()):
        path, _, line = ref.rpartition(":")
        try:
            removal_line = int(line)
        except ValueError:
            continue
        full = next((p for p in report.changelogs if p.endswith(path)), None)
        if not full:
            continue
        text = common.read_text(full)
        rx = _whole(name)
        for i, ln in enumerate(text.splitlines(), 1):
            if i >= removal_line:
                break
            if rx.search(ln):
                report.resurrected.append(
                    f"{name}: mentioned at {path}:{i}, which is newer than the removal line {removal_line}"
                )
                report.removed_names.pop(name, None)
                break


def search_mentions(root: str, report: Report) -> None:
    drop_resurrected(report)
    # A name named as a successor even once is not treated as removed: it is alive.
    for n in sorted(report.survivors & set(report.removed_names)):
        report.renamed_away.append(f"{n}: named as a successor, therefore alive")
        report.removed_names.pop(n, None)
    names = report.removed_names
    if not names:
        return
    whole = {n: _whole(n) for n in names}
    changelogs = {os.path.relpath(p, root) for p in report.changelogs}
    code_hits: Set[str] = set()

    for dirpath, dirs, fnames in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for n in sorted(fnames):
            ext = real_ext(n)
            path = os.path.join(dirpath, n)
            rel = os.path.relpath(path, root)
            if rel in changelogs or CHANGELOG_NAME.match(n):
                continue
            if ext not in DOC_EXT + CONFIG_EXT + CODE_EXT:
                continue
            try:
                if os.path.getsize(path) > 4_000_000:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                continue
            report.files_searched += 1
            is_doc = ext in DOC_EXT + CONFIG_EXT
            old = bool(OLD_DOCS.search(rel))

            for i, line in enumerate(lines, 1):
                for name in names:
                    if name not in line or not whole[name].search(line):
                        continue
                    if not is_doc:
                        code_hits.add(name)
                        continue
                    if old:
                        report.old_docs.append(f"{name} at {rel}:{i}: older documentation version")
                        continue
                    window = "\n".join(lines[max(0, i - 3) : i + 2])
                    if QUALIFIED.search(window):
                        report.qualified.append(f"{name} at {rel}:{i}: the mention is qualified")
                        continue
                    cref, section = names[name]
                    report.findings.append(
                        Finding(
                            name=name,
                            hard=True,
                            where=f"{rel}:{i}",
                            changelog_ref=cref,
                            section=section,
                            line_text=line.strip()[:120],
                        )
                    )

    # A name still defined in the code cannot be treated as removed
    kept = []
    for f in report.findings:
        if f.name in code_hits:
            report.still_in_code.append(f"{f.name}: present in the code, the removal was partial")
            f.hard = False
        kept.append(f)
    report.findings = kept


def analyse(root: str, report: Report) -> None:
    report.changelogs = find_changelogs(root)
    for p in report.changelogs:
        extract_removed(p, root, report)
    search_mentions(root, report)


# --------------------------------------------------------------------------


def print_report(report: Report, verbose: bool = False) -> None:
    hard = [f for f in report.findings if f.hard]
    soft = [f for f in report.findings if not f.hard]

    def block(title: str, items: List[Finding]) -> None:
        if not items:
            return
        print(f"\n=== {title} ({len(items)}) ===")
        for f in items:
            print(f"\n[{f.name}]")
            print(f"  still promised at:  {f.where}")
            print(f"    {f.line_text}")
            print(f"  removed per changelog: {f.changelog_ref}")
            print(f"    section: {f.section[:70]}")
            for d in f.detail:
                print(f"  {d}")

    block("Removed and still promised", hard)
    block("Needs reading by a human", soft)

    print("\n=== Coverage ===")
    print(f"  changelogs:             {len(report.changelogs)}")
    print(f"  removal sections:       {report.sections}")
    print(f"  removed names:          {len(report.removed_names)}")
    print(f"  files searched:         {report.files_searched}")
    print(f"  name far from the verb: {len(report.far_from_verb)} (something about it went away)")
    print(f"  successor names:        {len(report.renamed_away)} (right of 'is now', therefore alive)")
    print(f"  scenario, not the name: {len(report.scoped)} ('restart from --step ... is removed')")
    print(f"  back or alive:          {len(report.resurrected)} (mentioned newer than the removal)")
    print(f"  truncated by the parser:{len(report.truncated)} (the name continues on the line)")
    print(f"  mention is qualified:   {len(report.qualified)} (version, before, no longer)")
    print(f"  older documentation:    {len(report.old_docs)}")
    print(f"  still present in code:  {len(report.still_in_code)}")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, []))

    if verbose:
        for title, items in (
            ("Name far from the removal verb", report.far_from_verb),
            ("Successor name rather than a removed one", report.renamed_away),
            ("A scenario went away, not the name", report.scoped),
            ("Back or alive", report.resurrected),
            ("Truncated by the parser", report.truncated),
            ("Mention qualified by version", report.qualified),
            ("Older documentation", report.old_docs),
            ("Still present in the code", report.still_in_code),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items[:40]:
                    print(f"  {i}")
                if len(items) > 40:
                    print(f"  ... and {len(items) - 40} more")
        if report.removed_names:
            print(f"\n--- Removed names from the changelog ({len(report.removed_names)}) ---")
            for n, (ref, sec) in sorted(report.removed_names.items())[:40]:
                print(f"  {n}  ({ref})")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Removed and still promised")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    report = Report()
    analyse(args.dir, report)
    print_report(report, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "name": f.name, "hard": f.hard, "where": f.where,
                        "changelog": f.changelog_ref, "section": f.section,
                        "line": f.line_text,
                    }
                    for f in report.findings
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(f.hard for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
