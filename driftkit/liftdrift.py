#!/usr/bin/env python3
"""liftdrift.py: copies that fell behind an upstream fix.

The species: a project copied a piece of someone else's code, upstream later
found a defect there and fixed it, and the copy kept the old bug. A defect like
this is already proven by somebody else, so a maintainer needs no convincing,
only the upstream commit.

HOW IT WORKS. Comparing end states is useless, checked by hand: of 71 functions
in karmada, 53 differ from today's Kubernetes, and almost all of it turned out to
be upstream evolution rather than a bug fix. `GetDeletableResources` gained a
context, `IsStandardResourceName` changed a parameter type, and neither bothers
the copy.

So the question is put differently. Which commits upstream after the copy point
look like a bug fix, and is that bug present in the copy. The order of work:

  1. find the lifted-code markers in the tree (+lifted:source, lifted from,
     copied from, vendored from, adapted from) and bind each one to the
     declaration below it, which gives the coordinate inside the copy;
  2. take the original from EXACTLY the version it was copied from rather than
     master, otherwise upstream evolution gets mistaken for a stale copy;
  3. when the body of the symbol matches between that version and the current
     one, stay quiet: upstream never touched it;
  4. when it differs, walk the upstream commits for that file after the copy
     point and keep those that look like a fix by their wording,
     fix, bug, security, panic, overflow, leak, race, nil, CVE,
     then keep only those that really changed THIS symbol, by comparing the body
     before and after the commit rather than by reading the message;
  5. check the content for the fix inside the copy. The lines the commit added
     are searched for in the copy. It is a finding when none of them are found
     AND the copy matches the version BEFORE the fix, word for word, by at least
     OVERLAP_MIN. Additionally requiring the copy to hold the lines the commit
     REMOVED would be wrong: an ordinary fix adds a check and removes nothing,
     and that requirement would hide most findings. But when a fix did remove
     something and the copy has none of it, the copy does not contain the code
     that was being fixed, so the fix does not apply to it at all.

Step 5 is the guard against lying in our own favour: the conclusion comes from
the code of the copy rather than from dates or words in a commit message. The
overlap threshold guards from the other side: when a copy was rewritten, the
absence of a fix proves nothing, and such cases go to "inconclusive".

RENAMES. Names often change when code is copied: `ValidateFederatedHPA` in
karmada is `ValidateHorizontalPodAutoscaler` in Kubernetes. Searching by the name
of the copy is useless, so the symbol is taken by the line range from the link
itself, which is correct for exactly the version it was copied from. When two
declarations fall equally close to that range, the tool does not guess and says
"symbol not found".

KNOWN BLIND SPOTS, stated plainly:
  - a symbol renamed beyond what the line range from the link can recover: on
    karmada that is 14 of 142, and they are printed by name;
  - a fix with no marker word in the commit subject: the word filter is crude;
  - a copy rewritten past the overlap threshold: there the absence of a fix
    proves nothing, and such cases go to "inconclusive";
  - any language other than Go: the declaration parser is written for it.

WHICH PROJECTS THIS SUITS (measured on three of them).

The yield depends on whether a project keeps a PER-SYMBOL registry of lifted
code rather than on its size. The link has to carry a repository, a version and
a file; "copied from Kubernetes" in a file header is enough for nothing.

  karmada  142 usable markers against  13 unusable -> 3 findings
  thanos    13 usable          against  63 unusable -> 0
  argo-cd    5 usable          against 101 unusable -> 0

karmada has `hack/update-lifted`, the registry in `pkg/util/lifted/doc.go` and a
`+lifted:source` marker above every symbol. thanos and argo-cd have prose in file
headers and links to commits and issues, which give nothing to compare against.

Hence the rule for picking a project: count the per-symbol markers with a blob
link first. Below a few dozen, move on. The tool will find nothing there, and not
because the copies are fine but because there is nothing to compare them with.

Dependencies: an authenticated `gh`. Every GitHub answer is cached on disk, so a
repeat run does not touch the network.

Run:
  python3 liftdrift.py --dir ~/Projects/oss/k8s/karmada/pkg/util/lifted
  python3 liftdrift.py --dir ... -v          # list what was parsed, by name
  python3 liftdrift.py --dir ... --json out.json

Tests: test_liftdrift.py and test_gosym.py next to this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import stamp  # noqa: E402

import gosym  # noqa: E402

DEFAULT_CACHE = os.path.expanduser("~/.cache/liftdrift")

# How closely the body of a copy has to match the version BEFORE the fix for any
# claim about it to hold. Below the threshold the copy was rewritten and the
# absence of a fix means nothing.
OVERLAP_MIN = 0.6

# --------------------------------------------------------------------------
# Cached GitHub access
# --------------------------------------------------------------------------


def _decode_stream(text: str) -> Any:
    """Parse a stream of concatenated JSON values into one list.

    `gh api --paginate` joins pages with no separator: `[...][...]` for arrays
    and `{...}{...}` for objects such as `search/issues`. The old fix replaced
    "][" with a comma, which broke on objects, and the review harvester failed to
    parse on its very first live run.
    """
    dec = json.JSONDecoder()
    out: List[Any] = []
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i].isspace():
            i += 1
        if i >= n:
            break
        try:
            val, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break
        if isinstance(val, list):
            out.extend(val)
        else:
            out.append(val)
    return out


class GitHub:
    """A thin wrapper around gh with an on-disk cache.

    The cache is mandatory: without it a repeat run costs hundreds of requests and
    tests are impossible. Errors are cached too, to stop hammering the network.
    """

    def __init__(self, cache_dir: str = DEFAULT_CACHE, offline: bool = False):
        self.cache_dir = cache_dir
        self.offline = offline
        self.calls = 0
        self.cache_hits = 0
        os.makedirs(cache_dir, exist_ok=True)

    def _key(self, kind: str, arg: str) -> str:
        return os.path.join(
            self.cache_dir, f"{kind}-{hashlib.sha1(arg.encode()).hexdigest()}"
        )

    def _run(self, args: Sequence[str]) -> Tuple[int, str]:
        self.calls += 1
        res = subprocess.run(["gh", *args], capture_output=True, text=True)
        return res.returncode, res.stdout if res.returncode == 0 else res.stderr

    def api(self, path: str, paginate: bool = False) -> Any:
        key = self._key("api" + ("-p" if paginate else ""), path)
        if os.path.exists(key):
            self.cache_hits += 1
            with open(key, encoding="utf-8") as fh:
                return json.load(fh)
        if self.offline:
            raise LookupError(f"not in cache and network disabled: {path}")
        args = ["api", path] + (["--paginate"] if paginate else [])
        code, out = self._run(args)
        if code != 0:
            data: Any = {"__error__": out.strip()[:300]}
        else:
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                # `gh api --paginate` joins pages back to back: `[...][...]` for
                # arrays and `{...}{...}` for objects such as search/issues.
                # Joining by replacing "][" broke on objects, so the stream of
                # values is parsed rather than patched as a string.
                data = _decode_stream(out)
        with open(key, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return data

    def raw_file(self, repo: str, path: str, ref: str) -> Optional[str]:
        arg = f"{repo}@{ref}:{path}"
        key = self._key("raw", arg)
        if os.path.exists(key):
            self.cache_hits += 1
            with open(key, encoding="utf-8") as fh:
                txt = fh.read()
            return None if txt == "\0MISSING" else txt
        if self.offline:
            raise LookupError(f"not in cache and network disabled: {arg}")
        code, out = self._run(
            [
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{repo}/contents/{path}?ref={ref}",
            ]
        )
        txt = out if code == 0 else "\0MISSING"
        with open(key, "w", encoding="utf-8") as fh:
            fh.write(txt)
        return None if code != 0 else out

    def default_branch(self, repo: str) -> str:
        d = self.api(f"repos/{repo}")
        return d.get("default_branch", "master") if isinstance(d, dict) else "master"

    def branch_point_date(self, repo: str, ref: str) -> Optional[str]:
        """The date the branch was taken off the main line.

        Taking the tip of a release branch is wrong: backports keep landing there
        for years, and half the fixes in master would fall "before" that date and
        be lost.
        """
        dflt = self.default_branch(repo)
        if ref == dflt:
            return None
        cmp_ = self.api(f"repos/{repo}/compare/{ref}...{dflt}?per_page=1")
        if not isinstance(cmp_, dict):
            return None
        base = cmp_.get("merge_base_commit") or {}
        return (base.get("commit") or {}).get("committer", {}).get("date")

    def commits_for_path(
        self, repo: str, path: str, since: Optional[str], limit: int = 300
    ) -> Tuple[List[dict], bool]:
        dflt = self.default_branch(repo)
        q = f"repos/{repo}/commits?path={path}&sha={dflt}&per_page=100"
        if since:
            q += f"&since={since}"
        data = self.api(q, paginate=True)
        if not isinstance(data, list):
            return [], False
        truncated = len(data) > limit
        return data[:limit], truncated


# --------------------------------------------------------------------------
# Finding lifted code
# --------------------------------------------------------------------------

_MARKER_STRICT = re.compile(r"\+lifted:source\s*=", re.I)
_MARKER_PROSE = re.compile(
    r"\b(?:lifted|copied|vendored|borrowed|adapted|forked)\s+from\b|\bbased\s+on\b",
    re.I,
)
_MARKER = re.compile(_MARKER_STRICT.pattern + "|" + _MARKER_PROSE.pattern, re.I)
_URL = re.compile(r"https://github\.com/\S+")
_COMMENT = re.compile(r"^\s*(//|\*|/\*)")


@dataclass
class Borrow:
    copy_file: str
    marker_line: int
    decl: gosym.Decl
    repo: str
    ref: str
    path: str
    url: str
    changed: bool
    line_range: Optional[Tuple[int, int]] = None


def discover(root: str, lookahead: int = 3) -> Tuple[List[Borrow], List[str], List[str]]:
    """Finds lifted-code markers and binds each to the declaration below it.

    Returns (borrows, unparsed, notes without a link).

    Unparsed markers get printed: a silently lost marker looks like a clean
    report. A phrase such as "This code is lifted from the Kubernetes codebase" in
    a file header is no loss though: it marks no symbol, it explains something to
    a reader, and it carries no link by design. Those go into a separate list so
    they do not look like a broken parser.
    """
    found: List[Borrow] = []
    unparsed: List[str] = []
    notes: List[str] = []
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            if not name.endswith(".go"):
                continue
            full = os.path.join(dirpath, name)
            with open(full, encoding="utf-8", errors="replace") as fh:
                src = fh.read()
            lines = src.splitlines()
            for i, ln in enumerate(lines, 1):
                if not _MARKER.search(ln) or not _COMMENT.match(ln):
                    continue
                url = None
                for j in range(i - 1, min(len(lines), i - 1 + lookahead)):
                    if not _COMMENT.match(lines[j]):
                        break
                    m = _URL.search(lines[j])
                    if m:
                        url = m.group(0).rstrip(".,;)")
                        break
                # An indented comment sits inside a function body: an explanation
                # along the way ("the constant 2 comes from there") rather than a
                # marker of a lifted declaration.
                inline = ln[: len(ln) - len(ln.lstrip())] != ""
                if not url:
                    if _MARKER_STRICT.search(ln):
                        unparsed.append(f"{full}:{i}: a +lifted:source marker with no link")
                    else:
                        notes.append(f"{full}:{i}: a mention of lifted code with no link")
                    continue
                parsed = gosym.parse_github_blob(url)
                if not parsed:
                    # A link to a commit, an issue or a pull request. Nothing to
                    # compare against: neither a file nor a version is there.
                    notes.append(f"{full}:{i}: the link does not point at a file: {url}")
                    continue
                if inline:
                    notes.append(f"{full}:{i}: an explanation inside a function body, no symbol marker")
                    continue
                repo, ref, path, rng = parsed
                decl = gosym.next_after(src, i)
                if decl is None:
                    unparsed.append(f"{full}:{i}: no declaration below the marker")
                    continue
                changed = any(
                    "+lifted:changed" in lines[k]
                    for k in range(i - 1, min(len(lines), i + lookahead))
                )
                found.append(
                    Borrow(
                        copy_file=full,
                        marker_line=i,
                        decl=decl,
                        repo=repo,
                        ref=ref,
                        path=path,
                        url=url,
                        changed=changed,
                        line_range=rng,
                    )
                )

    # One declaration is often marked twice: by a file header with a link and by
    # its own +lifted:source. Counting that as two borrows doubles both the borrow
    # count and the findings. The marker closest to the declaration wins.
    best: Dict[Tuple[str, int, str], Borrow] = {}
    for b in found:
        key = (b.copy_file, b.decl.start, b.decl.name)
        cur = best.get(key)
        if cur is None or b.marker_line > cur.marker_line:
            if cur is not None and cur.changed:
                b.changed = True
            best[key] = b
        elif b.changed:
            best[key].changed = True
    return sorted(best.values(), key=lambda x: (x.copy_file, x.decl.start)), unparsed, notes


# --------------------------------------------------------------------------
# Selecting commits that look like fixes
# --------------------------------------------------------------------------

FIX_WORDS = (
    "fix", "fixes", "fixed", "bug", "bugfix", "security", "panic", "panics",
    "overflow", "leak", "leaks", "leaking", "race", "deadlock", "nil",
    "npe", "regression", "crash", "corrupt", "corruption",
)
_FIX = re.compile(r"\b(" + "|".join(FIX_WORDS) + r")\b|\bCVE-\d{4}-\d+\b", re.I)


def looks_like_fix(message: str) -> bool:
    """The first line of the message only: half the words in the body of a k8s
    commit come from the release-note template and anything matches them.

    Word boundaries are mandatory: without them `fix` turns up inside `prefix`,
    `suffix` and `fixture`, and the filter stops filtering.
    """
    return bool(_FIX.search(message.split("\n")[0]))


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------


@dataclass
class Finding:
    symbol: str
    copy_ref: str
    upstream_url: str
    commit_sha: str
    commit_subject: str
    commit_url: str
    added_total: int
    added_in_copy: int
    removed_total: int
    removed_in_copy: int
    overlap: float  # share of the pre-fix body matching the copy word for word
    changed_flag: bool
    detail: List[str] = dc_field(default_factory=list)

    @property
    def confident(self) -> bool:
        # Requiring removed lines unconditionally would be wrong: an ordinary fix
        # adds a check and removes nothing, and such a requirement would hide most
        # real findings.
        # But IF a fix did remove something and the copy has none of it, the copy
        # does not contain the code that was being fixed either. The fix simply
        # does not apply to it.
        if self.removed_total > 0 and self.removed_in_copy == 0:
            return False
        return (
            self.added_total > 0
            and self.added_in_copy == 0
            and self.overlap >= OVERLAP_MIN
            and not self.changed_flag
        )


@dataclass
class Report:
    borrows: int = 0
    unparsed: List[str] = dc_field(default_factory=list)
    prose_notes: List[str] = dc_field(default_factory=list)
    upstream_untouched: List[str] = dc_field(default_factory=list)
    symbol_not_found: List[str] = dc_field(default_factory=list)
    no_fix_commits: List[str] = dc_field(default_factory=list)
    already_patched: List[str] = dc_field(default_factory=list)
    no_such_code: List[str] = dc_field(default_factory=list)
    inconclusive: List[str] = dc_field(default_factory=list)
    truncated: List[str] = dc_field(default_factory=list)
    findings: List[Finding] = dc_field(default_factory=list)
    api_calls: int = 0
    cache_hits: int = 0


def _body_at(gh: GitHub, repo: str, path: str, ref: str, name: str) -> Optional[str]:
    src = gh.raw_file(repo, path, ref)
    if src is None:
        return None
    d = gosym.find(src, name)
    return d.text if d else None


def _upstream_name(
    gh: GitHub, b: Borrow, slack: int = 4
) -> Tuple[Optional[str], Optional[str]]:
    """What the symbol is called upstream and its body in the copied version.

    Names often change when code is copied: `ValidateHorizontalPodAutoscaler`
    became
    `ValidateFederatedHPA`, `validateTaintEffect` - `validateClusterTaintEffect`.
    Searching by the name of the copy is useless. The link carries a line range
    though, correct for exactly the version the copy was made from, and that is
    what the declaration is taken by. No guessing: the lines come from the
    project's own registry.
    """
    src = gh.raw_file(b.repo, b.path, b.ref)
    if src is None:
        return None, None
    d = gosym.find(src, b.decl.name)
    if d is not None:
        return d.name, d.text
    if not b.line_range:
        return None, None
    lo, hi = b.line_range
    hits = [
        x
        for x in gosym.declarations(src)
        if lo - slack <= x.start <= hi + slack and x.kind == b.decl.kind
    ]
    if not hits:
        return None, None
    # Take the declaration starting closest to the first line of the range.
    # With two equally close, no guessing: the price of an error is an invented
    # finding.
    hits.sort(key=lambda x: abs(x.start - lo))
    if len(hits) > 1 and abs(hits[0].start - lo) == abs(hits[1].start - lo):
        return None, None
    return hits[0].name, hits[0].text


def _line_diff(before: str, after: str) -> Tuple[List[str], List[str]]:
    """Lines missing from the second and from the first. The comparison is
    normalised: indentation, comments and package names almost always change
    when code is copied."""
    b = gosym.norm_body(before, drop_qualifiers=True)
    a = gosym.norm_body(after, drop_qualifiers=True)
    bs, as_ = set(b), set(a)
    added = [x for x in a if x not in bs]
    removed = [x for x in b if x not in as_]
    return added, removed


def analyse(
    borrows: Sequence[Borrow], gh: GitHub, report: Report, max_commits: int = 300
) -> None:
    report.borrows = len(borrows)
    for b in borrows:
        tag = f"{b.decl.name} ({os.path.basename(b.copy_file)}:{b.decl.start})"

        up_name, old = _upstream_name(gh, b)
        if old is None or up_name is None:
            report.symbol_not_found.append(f"{tag}: not in {b.repo}@{b.ref}:{b.path}")
            continue
        if up_name != b.decl.name:
            tag += f" [upstream {up_name}]"
        dflt = gh.default_branch(b.repo)
        new = _body_at(gh, b.repo, b.path, dflt, up_name)
        if new is None:
            report.symbol_not_found.append(
                f"{tag}: gone from {b.repo}@{dflt}, moved or renamed"
            )
            continue

        # Upstream never touched the symbol, so there is nothing to look for.
        # This is the most common outcome and it has to stay cheap.
        if gosym.bodies_equal(old, new):
            report.upstream_untouched.append(tag)
            continue

        since = gh.branch_point_date(b.repo, b.ref)
        commits, truncated = gh.commits_for_path(b.repo, b.path, since, max_commits)
        if truncated:
            note = f"{b.repo}:{b.path}: more than {max_commits} commits, the newest were taken"
            if note not in report.truncated:
                report.truncated.append(note)
        fixes = [c for c in commits if looks_like_fix(c["commit"]["message"])]
        if not fixes:
            report.no_fix_commits.append(f"{tag}: upstream changed it, but not with fixes")
            continue

        copy_body = b.decl.text
        hit = False
        for c in fixes:
            parents = c.get("parents") or []
            # A merge commit carries the same diff as the real commit under it.
            # Counting both doubles a finding for nothing.
            if len(parents) != 1:
                continue
            after = _body_at(gh, b.repo, b.path, c["sha"], up_name)
            before = _body_at(gh, b.repo, b.path, parents[0]["sha"], up_name)
            if after is None or before is None:
                continue
            # The commit has to have touched THIS symbol. Words in the message
            # prove nothing: a single k8s commit holds a dozen functions.
            if gosym.bodies_equal(before, after):
                continue
            added, removed = _line_diff(before, after)
            if not added and not removed:
                continue
            copy_lines = set(gosym.norm_body(copy_body, drop_qualifiers=True))
            a_in = sum(1 for x in added if x in copy_lines)
            r_in = sum(1 for x in removed if x in copy_lines)
            before_lines = gosym.norm_body(before, drop_qualifiers=True)
            overlap = (
                sum(1 for x in before_lines if x in copy_lines) / len(before_lines)
                if before_lines
                else 0.0
            )
            subject = c["commit"]["message"].split("\n")[0][:120]
            f = Finding(
                symbol=b.decl.name,
                copy_ref=f"{b.copy_file}:{b.decl.start}",
                upstream_url=b.url,
                commit_sha=c["sha"][:12],
                commit_subject=subject,
                commit_url=f"https://github.com/{b.repo}/commit/{c['sha']}",
                added_total=len(added),
                added_in_copy=a_in,
                removed_total=len(removed),
                removed_in_copy=r_in,
                overlap=overlap,
                changed_flag=b.changed,
                detail=(
                    [f"  upstream removed: {x}" for x in removed[:4]]
                    + [f"  upstream added:   {x}" for x in added[:4]]
                ),
            )
            if a_in > 0:
                report.already_patched.append(
                    f"{tag}: {f.commit_sha} is already in the copy ({a_in} of {len(added)} lines)"
                )
            elif not added:
                report.inconclusive.append(
                    f"{tag}: {f.commit_sha} only removed lines, nothing to judge by"
                )
            elif removed and r_in == 0:
                report.no_such_code.append(
                    f"{tag}: {f.commit_sha} fixed code the copy does not have "
                    f"(none of the {len(removed)} removed lines)"
                )
            elif overlap < OVERLAP_MIN:
                report.inconclusive.append(
                    f"{tag}: {f.commit_sha}: the copy matches the pre-fix version "
                    f"by only {overlap:.0%}, it was rewritten"
                )
            else:
                report.findings.append(f)
                hit = True
        if not hit:
            continue
    report.api_calls = gh.calls
    report.cache_hits = gh.cache_hits


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def print_report(report: Report, verbose: bool = False) -> None:
    confident = [f for f in report.findings if f.confident]
    weak = [f for f in report.findings if not f.confident]

    def block(title: str, items: List[Finding]) -> None:
        if not items:
            return
        print(f"\n=== {title} ({len(items)}) ===")
        for f in items:
            print(f"\n[{f.symbol}]")
            print(f"  copy:      {f.copy_ref}")
            print(f"  original:  {f.upstream_url}")
            print(f"  fix:       {f.commit_url}")
            print(f"             {f.commit_subject}")
            print(
                f"  check:     added lines present in the copy {f.added_in_copy} of {f.added_total}, "
                f"removed lines still in the copy {f.removed_in_copy} of {f.removed_total}, "
                f"copy matches the pre-fix version by {f.overlap:.0%}"
            )
            if f.changed_flag:
                print("  caveat:    the copy is marked changed, it was edited locally")
            for d in f.detail:
                print(f"  {d}")

    block("Copy fell behind the original", confident)
    block("Needs reading by a human", weak)

    print("\n=== Coverage ===")
    print(f"  borrows found:              {report.borrows}")
    print(f"  markers not parsed:         {len(report.unparsed)}")
    print(f"  not a symbol marker:        {len(report.prose_notes)} (file headers, commit links, inline notes)")
    print(f"  symbol not found upstream:  {len(report.symbol_not_found)}")
    print(f"  upstream never touched it:  {len(report.upstream_untouched)}")
    print(f"  changed, but not by fixes:  {len(report.no_fix_commits)}")
    print(f"  fix already in the copy:    {len(report.already_patched)}")
    print(f"  fixed code that is absent:  {len(report.no_such_code)}")
    print(f"  inconclusive:               {len(report.inconclusive)}")
    for t in report.truncated:
        print(f"  truncated:                  {t}")
    print(f"  GitHub requests:            {report.api_calls} (from cache {report.cache_hits})")
    print(common.findings_line(len(confident), len(weak)))
    print(stamp.line(__file__, ['gosym.py']))

    if verbose:
        for title, items in (
            ("Markers not parsed", report.unparsed),
            ("Mentions unusable for comparison", report.prose_notes),
            ("Symbol not found upstream", report.symbol_not_found),
            ("Upstream never touched it", report.upstream_untouched),
            ("Changed, but not by fixes", report.no_fix_commits),
            ("Fix already in the copy", report.already_patched),
            ("Fixed code the copy does not have", report.no_such_code),
            ("Inconclusive", report.inconclusive),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items:
                    print(f"  {i}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Copies that fell behind an upstream fix")
    ap.add_argument("--dir", required=True, help="directory holding the copies")
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="directory of the GitHub response cache")
    ap.add_argument("--offline", "--no-proof", dest="offline", action="store_true",
                    help="cache only, never touch the network")
    ap.add_argument("--max-commits", type=int, default=300)
    ap.add_argument("--json", help="write findings to JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    gh = GitHub(args.cache, args.offline)
    borrows, unparsed, notes = discover(args.dir)
    report = Report(unparsed=unparsed, prose_notes=notes)
    analyse(borrows, gh, report, args.max_commits)
    print_report(report, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "symbol": f.symbol,
                        "copy": f.copy_ref,
                        "upstream": f.upstream_url,
                        "commit": f.commit_url,
                        "subject": f.commit_subject,
                        "hard": f.confident,
                        "confident": f.confident,  # legacy key, kept for compatibility
                        "added_in_copy": f.added_in_copy,
                        "added_total": f.added_total,
                        "removed_in_copy": f.removed_in_copy,
                        "removed_total": f.removed_total,
                        "overlap": round(f.overlap, 3),
                        "changed_flag": f.changed_flag,
                        "detail": f.detail,
                    }
                    for f in report.findings
                ],
                fh,
                ensure_ascii=False,
                indent=1,
            )
    return 1 if any(f.confident for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
