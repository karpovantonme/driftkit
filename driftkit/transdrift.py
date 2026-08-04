#!/usr/bin/env python3
"""transdrift.py: translations that fell behind the original.

The same species as `liftdrift.py`, except the copy here is a translation rather
than code: the original changed, the translation kept the old content and lies
to its reader.

HOW IT WORKS. Prose cannot be compared: it is in another language and any text
diff shows a hundred percent difference. The **skeleton of a page survives
translation** though: code blocks, external links, image paths and the sequence
of headings stay the same in any language. That is what gets compared.

The order:
  1. pair original and translation files by relative path;
  2. take the skeleton of both (`mdskel.py`);
  3. find elements present in the original and absent from the translation;
  4. prove this is drift rather than a translator decision: find the commit that
     added the element to the original and show it landed after the point the
     translation was made from.

Step 4 is what separates a finding from a nitpick. Without it a maintainer can
fairly answer "we shortened it deliberately". With a commit the conversation is
different: the section appeared on this date and the translation has not been
touched since.

THE REFERENCE POINT comes from one of two places:
  - an explicit marker in the front matter (`default_lang_commit` in
    OpenTelemetry, `source_commit`, `sourceCommit`), the most reliable case;
  - with no marker, the date of the last commit that touched the translation
    file. Weaker: that edit may have been cosmetic. Such findings are marked
    separately.

WHAT THE TOOL DOES NOT CALL A MISMATCH, because it is normal in translation:
  - different comments inside code examples (they get translated, correctly);
  - a link to the local locale: `/ja/docs/x` against `/docs/x`;
  - different heading text with the same count and levels;
  - a placeholder page saying "not translated yet".

And separately, from the experience of earlier tools: when a translation is
missing more than half of the skeleton, that is **one** trouble, the translation
is incomplete as a whole, rather than N missing elements. Printing N means lying
in our own favour N times over.

KNOWN BLIND SPOTS:
  - it does not read prose, so a paragraph rewritten in the original while the
    translation kept the old meaning stays invisible;
  - it does not judge a page with no explicit source marker beyond the date of
    the last edit, and that edit may have been cosmetic;
  - it does not parse anything other than markdown.

Dependencies: an authenticated `gh` for the commit proof. Without it the tool
still works and the findings are marked as unproven.

Run:
  python3 transdrift.py --original content/en --translation content/ja \\
      --repo open-telemetry/opentelemetry.io
  python3 transdrift.py ... -v          # list what was dismissed, by name
  python3 transdrift.py ... --no-proof  # no network, skeleton only

Tests: test_transdrift.py and test_mdskel.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stamp  # noqa: E402

import mdskel  # noqa: E402
from liftdrift import GitHub, DEFAULT_CACHE  # noqa: E402

# Below this share of skeleton overlap the translation counts as incomplete as a
# whole and gets one line instead of a list of missing elements.
COMPLETENESS_MIN = 0.5

# A block shorter than this many meaningful lines cannot be judged: one
# translated comment outweighs the whole block and turns it into a "loss".
MIN_BLOCK_LINES = 3

# Above this many losses on one page we print one line with a list rather than N
# findings. The lesson from the interface comparator: many losses in one place
# are one trouble. Usually a single stale list or table sits behind it: the
# Chinese translation of supported-libraries.md gave 17 links that way.
MAX_PER_PAGE = 3

# Source-version markers seen in front matter
SOURCE_KEYS = ("default_lang_commit", "source_commit", "sourceCommit", "source_sha")


@dataclass
class Finding:
    kind: str
    hard: bool
    page: str
    original_ref: str
    translation_ref: str
    message: str
    proof: str = ""
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    pairs: int = 0
    only_original: List[str] = dc_field(default_factory=list)
    only_translation: List[str] = dc_field(default_factory=list)
    in_sync: List[str] = dc_field(default_factory=list)
    stubs: List[str] = dc_field(default_factory=list)
    incomplete: List[str] = dc_field(default_factory=list)
    no_anchor: List[str] = dc_field(default_factory=list)
    unproven: List[str] = dc_field(default_factory=list)
    findings: List[Finding] = dc_field(default_factory=list)
    api_calls: int = 0
    cache_hits: int = 0


# --------------------------------------------------------------------------


def pair_files(original: str, translation: str) -> Tuple[List[Tuple[str, str, str]], List[str], List[str]]:
    """Pairs sharing the same relative path."""

    def collect(root: str) -> Dict[str, str]:
        out = {}
        for dirpath, _dirs, names in os.walk(root):
            for n in sorted(names):
                if n.endswith((".md", ".markdown")):
                    full = os.path.join(dirpath, n)
                    out[os.path.relpath(full, root)] = full
        return out

    o, t = collect(original), collect(translation)
    pairs = [(rel, o[rel], t[rel]) for rel in sorted(set(o) & set(t))]
    return pairs, sorted(set(o) - set(t)), sorted(set(t) - set(o))


def source_anchor(sk: mdskel.Skeleton) -> Optional[str]:
    for key in SOURCE_KEYS:
        v = sk.front_matter.get(key)
        if v:
            return v
    return None


def _lines(body: str) -> List[str]:
    return [x for x in mdskel.norm_code(body).splitlines() if x]


def best_containment(orig_body: str, trans_blocks: Sequence[Tuple[str, str, int]]) -> float:
    """How well the best translated block covers the lines of the original one.

    Blocks cannot be compared word for word. Code examples get their full-line
    comments translated and their trailing comments too
    (`... # Other plugins that have been enabled`), and one such line makes the
    whole block look "missing". The first run over the Japanese translation of
    opentelemetry.io produced 166 false findings out of 170 this way.
    """
    o = _lines(orig_body)
    if not o:
        return 1.0
    oset = set(o)
    best = 0.0
    for _lang, body, _line in trans_blocks:
        t = set(_lines(body))
        if not t:
            continue
        best = max(best, len(oset & t) / len(oset))
    return best


def compare_skeletons(
    orig: mdskel.Skeleton, trans: mdskel.Skeleton, block_min: float = 0.6
) -> Tuple[List[Tuple[str, str, int, float]], List[Tuple[str, str, int]], float]:
    """Returns (missing blocks, missing links, skeleton overlap ratio).

    Only what is **entirely** absent from the translation counts as missing. A
    block that is present with edits does not: a translation edits examples
    legitimately.
    """
    missing_code = []
    seen: set = set()
    for lang, body, line in orig.code_blocks:
        if lang.lower() in mdskel.PROSE_LANGS:
            continue
        body_lines = _lines(body)
        if len(body_lines) < MIN_BLOCK_LINES:
            continue
        key = "\n".join(body_lines)
        if key in seen:
            continue
        seen.add(key)
        ratio = best_containment(body, trans.code_blocks)
        if ratio < block_min:
            missing_code.append((lang, body, line, ratio))
    o_code_count = len(seen)

    o_link = {}
    for target, line in orig.links + orig.images:
        if not mdskel.is_external(target):
            continue
        o_link.setdefault(mdskel.norm_link(target), (target, "", line))
    t_link = {mdskel.norm_link(t) for t, _ in trans.links + trans.images}
    missing_links = [v for k, v in o_link.items() if k not in t_link]

    total = o_code_count + len(o_link)
    matched = total - len(missing_code) - len(missing_links)
    ratio = (matched / total) if total else 1.0
    return missing_code, missing_links, ratio


# --------------------------------------------------------------------------
# Proof by commit
# --------------------------------------------------------------------------


def _commits_after(
    gh: GitHub, repo: str, path: str, since_sha: Optional[str], since_date: Optional[str]
) -> List[dict]:
    if since_sha and not since_date:
        c = gh.api(f"repos/{repo}/commits/{since_sha}")
        if isinstance(c, dict) and "commit" in c:
            since_date = c["commit"]["committer"]["date"]
    q = f"repos/{repo}/commits?path={path}&per_page=100"
    if since_date:
        q += f"&since={since_date}"
    data = gh.api(q, paginate=True)
    return data if isinstance(data, list) else []


def prove(
    gh: GitHub, repo: str, orig_path: str, needle: str, anchor_sha: Optional[str], anchor_date: Optional[str]
) -> Optional[dict]:
    """Finds the commit that added the sought line to the original after the reference point."""
    for c in _commits_after(gh, repo, orig_path, anchor_sha, anchor_date):
        parents = c.get("parents") or []
        if len(parents) != 1:  # a merge carries somebody else's diff
            continue
        detail = gh.api(f"repos/{repo}/commits/{c['sha']}")
        if not isinstance(detail, dict):
            continue
        for f in detail.get("files") or []:
            if f.get("filename") != orig_path:
                continue
            patch = f.get("patch") or ""
            for ln in patch.splitlines():
                if ln.startswith("+") and not ln.startswith("+++") and needle in ln:
                    return c
    return None


# --------------------------------------------------------------------------


def analyse(
    original_dir: str,
    translation_dir: str,
    repo: Optional[str],
    gh: Optional[GitHub],
    report: Report,
    repo_root: Optional[str] = None,
) -> None:
    pairs, only_o, only_t = pair_files(original_dir, translation_dir)
    report.pairs = len(pairs)
    report.only_original = only_o
    report.only_translation = only_t

    for rel, opath, tpath in pairs:
        with open(opath, encoding="utf-8", errors="replace") as fh:
            osrc = fh.read()
        with open(tpath, encoding="utf-8", errors="replace") as fh:
            tsrc = fh.read()
        osk, tsk = mdskel.parse(osrc), mdskel.parse(tsrc)

        # a "not translated yet" placeholder
        if tsk.body_lines < 5 or (osk.body_lines and tsk.body_lines / osk.body_lines < 0.15):
            report.stubs.append(f"{rel} (translation {tsk.body_lines} lines against {osk.body_lines})")
            continue

        missing_code, missing_links, ratio = compare_skeletons(osk, tsk)
        if not missing_code and not missing_links:
            report.in_sync.append(rel)
            continue

        # Many losses in one file are one trouble rather than N.
        # The lesson from the interface comparator: 115 "mismatches" instead of 9.
        if ratio < COMPLETENESS_MIN:
            report.incomplete.append(
                f"{rel}: {ratio:.0%} of the skeleton matches, the translation is incomplete as a whole"
            )
            continue

        anchor = source_anchor(tsk)
        if not anchor:
            report.no_anchor.append(rel)

        rel_orig_in_repo = os.path.join(repo_root, rel) if repo_root else None

        for lang, body, line, ratio in missing_code:
            needle = max((x for x in mdskel.norm_code(body).splitlines()), key=len, default="")
            f = Finding(
                kind="missing-code-block",
                hard=bool(anchor),
                page=rel,
                original_ref=f"{opath}:{line}",
                translation_ref=tpath,
                message=(
                    f"code block ({lang or 'no language'}, {len(body.splitlines())} lines) "
                    f"exists in the original; the closest one in the translation covers {ratio:.0%} of its lines"
                ),
                detail=[f"    {x}" for x in body.splitlines()[:4]],
            )
            _attach_proof(f, gh, repo, rel_orig_in_repo, needle, anchor, report)
            report.findings.append(f)

        for target, _unused, line in missing_links:
            f = Finding(
                kind="missing-link",
                hard=bool(anchor),
                page=rel,
                original_ref=f"{opath}:{line}",
                translation_ref=tpath,
                message=f"external link {target} exists in the original and not in the translation",
            )
            _attach_proof(f, gh, repo, rel_orig_in_repo, target, anchor, report)
            report.findings.append(f)

    _fold_by_page(report)

    if gh is not None:
        report.api_calls = gh.calls
        report.cache_hits = gh.cache_hits


def _fold_by_page(report: Report) -> None:
    """Folds a scatter of losses on one page into a single finding."""
    by_page: Dict[str, List[Finding]] = {}
    for f in report.findings:
        by_page.setdefault(f.page, []).append(f)
    out: List[Finding] = []
    for page, group in by_page.items():
        if len(group) <= MAX_PER_PAGE:
            out.extend(group)
            continue
        proven = [f for f in group if f.proof]
        out.append(
            Finding(
                kind="page-behind",
                hard=bool(proven),
                page=page,
                original_ref=group[0].original_ref.rsplit(":", 1)[0],
                translation_ref=group[0].translation_ref,
                message=(
                    f"the page fell behind as a whole: {len(group)} skeleton elements are missing "
                    "from the translation, which looks like one list or section never carried over"
                ),
                proof=proven[0].proof if proven else "",
                detail=[f"  {f.message[:110]}" for f in group[:8]]
                + ([f"  ... and {len(group) - 8} more"] if len(group) > 8 else []),
            )
        )
    report.findings = out


def _attach_proof(f: Finding, gh, repo, orig_path, needle, anchor, report: Report) -> None:
    if gh is None or not repo or not orig_path or not needle:
        f.hard = False
        report.unproven.append(f"{f.page} - {f.kind}: no commit proof was looked for")
        return
    c = prove(gh, repo, orig_path, needle, anchor, None)
    if c is None:
        f.hard = False
        report.unproven.append(
            f"{f.page} - {f.kind}: no commit adding this after the translation point was found"
        )
        return
    date = c["commit"]["committer"]["date"][:10]
    subj = c["commit"]["message"].split("\n")[0][:100]
    f.proof = f"https://github.com/{repo}/commit/{c['sha']}"
    f.detail.append(f"  added to the original on {date}: {subj}")


# --------------------------------------------------------------------------


def print_report(report: Report, verbose: bool = False) -> None:
    hard = [f for f in report.findings if f.hard]
    soft = [f for f in report.findings if not f.hard]

    def block(title: str, items: List[Finding]) -> None:
        if not items:
            return
        print(f"\n=== {title} ({len(items)}) ===")
        for f in items:
            print(f"\n[{f.kind}] {f.page}")
            print(f"  {f.message}")
            print(f"  original:    {f.original_ref}")
            print(f"  translation: {f.translation_ref}")
            if f.proof:
                print(f"  commit:      {f.proof}")
            for d in f.detail:
                print(f"  {d}")

    block("Translation fell behind the original", hard)
    block("Needs reading by a human", soft)

    print("\n=== Coverage ===")
    print(f"  page pairs:             {report.pairs}")
    print(f"  skeleton in sync:       {len(report.in_sync)}")
    print(f"  incomplete as a whole:  {len(report.incomplete)} (one line instead of a list)")
    print(f"  placeholders:           {len(report.stubs)}")
    print(f"  without a version mark: {len(report.no_anchor)}")
    print(f"  unproven by commit:     {len(report.unproven)}")
    print(f"  original only:          {len(report.only_original)}")
    print(f"  translation only:       {len(report.only_translation)}")
    print(f"  GitHub requests:        {report.api_calls} (from cache {report.cache_hits})")
    print(common.findings_line(len(hard), len(soft)))
    print(stamp.line(__file__, ['mdskel.py', 'liftdrift.py']))

    if verbose:
        for title, items in (
            ("Incomplete as a whole", report.incomplete),
            ("Placeholders", report.stubs),
            ("Without a version mark", report.no_anchor),
            ("Unproven by commit", report.unproven),
            ("Original only", report.only_original),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items[:60]:
                    print(f"  {i}")
                if len(items) > 60:
                    print(f"  ... and {len(items) - 60} more")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Translations that fell behind the original")
    ap.add_argument("--original", required=True, help="directory of the original")
    ap.add_argument("--translation", required=True, help="directory of the translation")
    ap.add_argument("--repo", help="owner/name, needed for the commit proof")
    ap.add_argument(
        "--repo-root",
        help="path of the original directory inside the repository when it differs "
        "from --original (for example content/en)",
    )
    ap.add_argument("--offline", "--no-proof", dest="no_proof", action="store_true",
                    help="stay offline; findings will remain soft")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    gh = None if args.no_proof else GitHub(args.cache)
    root = args.repo_root
    if root is None and args.repo:
        root = os.path.relpath(os.path.abspath(args.original), os.getcwd())
    report = Report()
    analyse(args.original, args.translation, args.repo, gh, report, root)
    print_report(report, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "kind": f.kind,
                        "hard": f.hard,
                        "page": f.page,
                        "original": f.original_ref,
                        "translation": f.translation_ref,
                        "message": f.message,
                        "proof": f.proof,
                        "detail": f.detail,
                    }
                    for f in report.findings
                ],
                fh,
                ensure_ascii=False,
                indent=1,
            )
    return 1 if hard_any(report) else 0


def hard_any(report: Report) -> bool:
    return any(f.hard for f in report.findings)


if __name__ == "__main__":
    sys.exit(main())
