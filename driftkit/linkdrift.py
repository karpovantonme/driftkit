#!/usr/bin/env python3
"""linkdrift.py: dead external links.

The highest-yield check in this kit and the only one **no linter anywhere
covers**, because it needs the network. Measurements: 47 dead links on
kotlin-web-site, 48 on weaviate, 22 across the nf-core ecosystem.

THE MAIN THING ABOUT HOW IT WORKS, and all of it comes from those three runs.
The raw result on kotlin-web-site was **123 "not 200"**, while 47 were real.
The difference is that "not 200" and "dead" are separate things:

  404, 410, 451: **dead**. That is a finding;
  403, 401, 429: bot protection or a paywall. The page is alive, we were simply
      not let in. NOT a finding, and this is the largest share of the noise;
  timeout, DNS failure, connection reset: **this may be our own network**. Those
      get a second pass, and if the failure repeats they go to "unverified"
      instead of findings. Someone else's page is not to blame for our link.

Hence the mandatory second pass over everything that did not answer the first
time. Without it the tool lies in its own favour by a factor of two and a half.

WHAT THE TOOL DOES NOT CHECK:
  - internal links: they are their own species with their own traps. Docs get
    assembled from several repositories and a file may arrive from elsewhere;
    54 "broken" links on kotlin-web-site burned on exactly that;
  - templated addresses: `{{ .Values.host }}`, `<your-domain>`, `%s`;
  - localhost, example.com, example.org and other deliberately illustrative hosts;
  - mailto, ftp and anything else that is not http.

And the kit-wide law: **one dead address repeated across fifty files is one
finding.** The report prints the address, how many times it occurs and the
coordinates of the first three.

Politeness towards other people's servers: requests go one at a time, with a
pause, HEAD first and GET only when HEAD is refused. Responses are cached on
disk, so a repeat run and a sweep over a hundred projects do not hit the same
addresses twice.

Run:
  python3 linkdrift.py --dir ~/Projects/oss/jb/kotlin-web-site
  python3 linkdrift.py --dir ... --offline     # cache only, never touch the network
  python3 linkdrift.py --dir ... --limit 200   # check at most this many addresses

Tests: test_linkdrift.py next to this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

DEFAULT_CACHE = os.path.expanduser("~/.cache/linkdrift")

# Dead, no questions
DEAD = {404, 410, 451}
# Alive, we were simply not let in. The largest share of noise in a raw run.
BLOCKED = {401, 402, 403, 405, 429, 503}

TEXT_EXT = (".md", ".rst", ".txt", ".adoc", ".yaml", ".yml", ".json", ".html", ".toml")

# Fixture directories hold saved copies of somebody else's pages, kept so that
# tests can parse them offline. Every address inside is a historical artefact
# and none of them is ours to fix. On astroquery a single saved SDSS page
# produced 57 of 101 findings.
_FIXTURE_DIR = re.compile(r"(^|/)(tests?|testing)/data/|(^|/)data/tests?/|"
                          r"(^|/)fixtures?/|(^|/)__snapshots__/", re.I)

# Working addresses printed inside an example of a tool's output: a temporary
# workspace, a session identifier, a job number. They are generated per run and
# were never meant to survive. 14 of 101 on astroquery.
_EPHEMERAL = re.compile(r"/workspace/|TMP_[A-Za-z0-9]{4,}|/tmp/|"
                        r"[?&](?:session|sid|token|jobid)=", re.I)

_URL = re.compile(r"https?://[^\s\)\]\}\"'`<>,;]+")
# A template inside the address: nothing to check
# A single `{` had to be a marker too. `}` is excluded from the address pattern,
# so a templated URL is captured up to the brace and arrives here as a stump:
# `https://login.microsoftonline.com/{tenant}/saml2` becomes `.../{tenant`, and
# that stump then 404s honestly. Matching the doubled form only meant three false
# findings on poweradmin. Reported by @darkdi, issue #2. Same species as the
# truncated flag name in deaddrift: a cut value that looks like a real one.
_TEMPLATED = re.compile(
    r"\{\{|\}\}|\{%|\{[A-Za-z_][\w-]*\}?|<[A-Za-z_-]+>|%s|\$\{|\$\(|YOUR[_-]|xxx", re.I)
# The host used to be anchored straight after the scheme, so `example.com` was
# caught and `www.example.com` was not: ten false findings out of eleven on
# php-curl-class. Reported by @darkdi, issue #3. Any subdomain is allowed now.
_PLACEHOLDER_HOST = re.compile(
    r"^https?://(?:[\w-]+\.)*(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|"
    r"example\.(?:com|org|net)|my(?:site|domain|host)\.|foo\.bar|test\.invalid)", re.I
)

# Identifiers that are URIs by construction and were never meant to resolve.
# XML and SAML namespaces, JSON-LD contexts, XML schemas. `schemas.xmlsoap.org`
# has not served content for years, and every SAML deployment still uses these
# exact strings verbatim. Eight false findings out of eleven on poweradmin,
# reported by @darkdi, issue #1.
_IDENTIFIER_URI = re.compile(
    r"^https?://(?:schemas\.xmlsoap\.org|schemas\.microsoft\.com|www\.w3\.org/\d{4}/|"
    r"purl\.org|xmlns\.com|docs\.oasis-open\.org/wss/|json-schema\.org/draft|"
    r"schema\.org/?$|ns\.adobe\.com|iptc\.org/std/)", re.I
)

UA = "Mozilla/5.0 (compatible; linkdrift/1.0; +https://github.com/)"

# Without an Accept header a single-page site cannot tell what is being asked
# for and answers 404 to a live page. crates.io does exactly that: the same
# URL is 404 with no Accept and 200 with one, for any user agent. That cost 26
# false "dead links" of 35 on comprehensive-rust before it was measured -- and
# the first diagnosis, that these hosts refuse robots by user agent, was wrong.
# Browsers send this string; so do we now.
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


@dataclass
class Finding:
    url: str
    hard: bool
    status: str
    count: int
    refs: List[str]
    detail: List[str] = dc_field(default_factory=list)


@dataclass
class Report:
    files: int = 0
    files_skipped: int = 0
    urls_found: int = 0
    urls_checked: int = 0
    templated: List[str] = dc_field(default_factory=list)
    identifier: List[str] = dc_field(default_factory=list)
    ephemeral: List[str] = dc_field(default_factory=list)
    fixture_files: int = 0
    placeholder: List[str] = dc_field(default_factory=list)
    alive: int = 0
    blocked: List[str] = dc_field(default_factory=list)
    unknown: List[str] = dc_field(default_factory=list)
    from_cache: int = 0
    requests: int = 0
    truncated: str = ""
    findings: List[Finding] = dc_field(default_factory=list)


# --------------------------------------------------------------------------


def collect(root: str, report: Report) -> Dict[str, List[str]]:
    """address -> coordinates of its occurrences."""
    found: Dict[str, List[str]] = defaultdict(list)
    for dirpath, _dirs, names in common.walk(root):
        for n in sorted(names):
            if not n.lower().endswith(TEXT_EXT):
                continue
            path = os.path.join(dirpath, n)
            if _FIXTURE_DIR.search(os.path.relpath(path, root).replace(os.sep, "/")):
                report.fixture_files += 1
                continue
            text = common.read_text(path)
            if not text:
                # Empty, unreadable, or over the size limit. Counted rather
                # than dropped: a file that disappears without a trace makes
                # the report look cleaner than the run was.
                report.files_skipped += 1
                continue
            report.files += 1
            rel = os.path.relpath(path, root)
            for i, line in enumerate(text.splitlines(), 1):
                for m in _URL.finditer(line):
                    url = m.group(0).rstrip(".,;:'\"")
                    found[url].append(f"{rel}:{i}")
    report.urls_found = len(found)
    return found


def worth_checking(url: str, report: Report) -> bool:
    if _TEMPLATED.search(url):
        report.templated.append(url)
        return False
    if _PLACEHOLDER_HOST.match(url):
        report.placeholder.append(url)
        return False
    if _IDENTIFIER_URI.match(url):
        report.identifier.append(url)
        return False
    if _EPHEMERAL.search(url):
        report.ephemeral.append(url)
        return False
    return True


# --------------------------------------------------------------------------


class Checker:
    """Address probing with an on-disk cache and a second pass over non-answers."""

    def __init__(self, cache_dir: str = DEFAULT_CACHE, offline: bool = False,
                 timeout: float = 12.0, pause: float = 0.3):
        self.cache_dir = cache_dir
        self.offline = offline
        self.timeout = timeout
        self.pause = pause
        self.requests = 0
        self.from_cache = 0
        os.makedirs(cache_dir, exist_ok=True)

    def _key(self, url: str) -> str:
        return os.path.join(self.cache_dir, hashlib.sha1(url.encode()).hexdigest())

    def cached(self, url: str) -> Optional[str]:
        p = self._key(url)
        if os.path.exists(p):
            self.from_cache += 1
            return common.read_text(p).strip()
        return None

    def _store(self, url: str, verdict: str) -> None:
        with open(self._key(url), "w", encoding="utf-8") as fh:
            fh.write(verdict)

    def _ask(self, url: str, method: str) -> str:
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": UA, "Accept": ACCEPT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return str(resp.status)
        except urllib.error.HTTPError as e:
            return str(e.code)
        except urllib.error.URLError as e:
            return f"net:{type(e.reason).__name__ if hasattr(e, 'reason') else 'URLError'}"
        except Exception as e:  # noqa: BLE001
            return f"net:{type(e).__name__}"

    def check(self, url: str) -> str:
        got = self.cached(url)
        if got is not None:
            return got
        if self.offline:
            return "unverified: network disabled"
        self.requests += 1
        verdict = self._ask(url, "HEAD")
        # Some servers do not do HEAD and answer 405 or 403 to it
        if verdict in ("405", "403", "501") or verdict.startswith("net:"):
            time.sleep(self.pause)
            self.requests += 1
            verdict = self._ask(url, "GET")
        time.sleep(self.pause)
        self._store(url, verdict)
        return verdict


def classify(verdict: str) -> str:
    if verdict.isdigit():
        code = int(verdict)
        if code in DEAD:
            return "dead"
        if code in BLOCKED:
            return "blocked"
        if 200 <= code < 400:
            return "alive"
        return "blocked"
    return "unverified"


# --------------------------------------------------------------------------


def analyse(root: str, report: Report, checker: Checker, limit: int = 0) -> None:
    found = collect(root, report)
    urls = [u for u in sorted(found) if worth_checking(u, report)]
    if limit and len(urls) > limit:
        report.truncated = f"{len(urls)} addresses, first {limit} checked"
        urls = urls[:limit]

    first: Dict[str, str] = {}
    for u in urls:
        first[u] = checker.check(u)
    report.urls_checked = len(urls)

    # Second pass over everything that did not answer: the failure may be ours.
    # Without it the raw result on kotlin-web-site was 123 instead of 47.
    retry = [u for u, v in first.items() if classify(v) == "unverified" and not v.startswith("unverified:")]
    for u in retry:
        os.path.exists(checker._key(u)) and os.remove(checker._key(u))
        first[u] = checker.check(u)

    for u in urls:
        verdict = first[u]
        kind = classify(verdict)
        refs = found[u]
        if kind == "alive":
            report.alive += 1
        elif kind == "blocked":
            report.blocked.append(f"{u} - {verdict}")
        elif kind == "unverified":
            report.unknown.append(f"{u} - {verdict}")
        else:
            report.findings.append(
                Finding(
                    url=u, hard=True, status=verdict, count=len(refs), refs=refs[:3],
                    detail=[f"  occurrences: {len(refs)}"] + [f"  {r}" for r in refs[:3]]
                    + ([f"  ... and {len(refs) - 3} more"] if len(refs) > 3 else []),
                )
            )
    report.requests = checker.requests
    report.from_cache = checker.from_cache


def print_report(report: Report, verbose: bool = False) -> None:
    if report.findings:
        print(f"\n=== Dead external links ({len(report.findings)}) ===")
        for f in sorted(report.findings, key=lambda x: -x.count):
            print(f"\n[{f.status}] {f.url}")
            for d in f.detail:
                print(d)

    print("\n=== Coverage ===")
    print(f"  files read:             {report.files}")
    print(f"  files skipped:          {report.files_skipped} (empty, unreadable or too large)")
    print(f"  addresses found:        {report.urls_found}")
    print(f"  addresses checked:      {report.urls_checked}")
    print(f"  templated:              {len(report.templated)} (nothing to check)")
    print(f"  illustrative hosts:     {len(report.placeholder)} (localhost, example.com)")
    print(f"  identifier URIs:        {len(report.identifier)} (XML/SAML namespaces, never addresses)")
    print(f"  ephemeral addresses:    {len(report.ephemeral)} (temporary workspaces, session ids)")
    print(f"  fixture files skipped:  {report.fixture_files} (saved copies of other people's pages)")
    print(f"  alive:                  {report.alive}")
    print(f"  not let in:             {len(report.blocked)} (403, 401, 429, page is alive)")
    print(f"  unverified:             {len(report.unknown)} (failure repeated, may be our own link)")
    print(f"  network requests:       {report.requests} (from cache {report.from_cache})")
    if report.truncated:
        print(f"  truncated:              {report.truncated}")
    print(common.findings_line(len(report.findings), 0))
    print(stamp.line(__file__, ['common.py']))

    if verbose:
        for title, items in (
            ("Not let in", report.blocked),
            ("Unverified", report.unknown),
            ("Templated", report.templated),
        ):
            if items:
                print(f"\n--- {title} ({len(items)}) ---")
                for i in items[:40]:
                    print(f"  {i}")
                if len(items) > 40:
                    print(f"  ... and {len(items) - 40} more")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Dead external links")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--limit", type=int, default=0, help="check at most this many addresses")
    ap.add_argument("--pause", type=float, default=0.3, help="pause between requests, seconds")
    common.add_common_args(ap, network=True)
    args = ap.parse_args(argv)

    report = Report()
    checker = Checker(args.cache, args.offline, pause=args.pause)
    analyse(args.dir, report, checker, args.limit)
    print_report(report, args.verbose)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "hard": f.hard, "url": f.url, "status": f.status,
                        "count": f.count, "refs": f.refs, "detail": f.detail,
                    }
                    for f in report.findings
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(f.hard for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
