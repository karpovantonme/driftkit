#!/usr/bin/env python3
"""lessons.py: harvesting review replies and turning them into rules.

The fourth stage of the pipeline and its only feedback loop.

WHY. Every maintainer remark of the form "this is not a defect, because X" is a
rule the kit does not have. Such remarks otherwise settle into working notes,
get read once and are forgotten. And this is exactly the knowledge that cannot
be obtained any other way: **someone else's rejection is worth more than our own
guess**, because it comes from a person who knows the project from inside.

HOW THIS DIFFERS FROM THE OTHER TOOLS. All the rest improve the kit exactly as
much as they get written. This one improves the kit by itself, as answers
arrive.

HOW IT WORKS.

  1. Walks our pull requests through `gh` and collects outcomes: merged, closed
     without a merge, rework requested, answered with a comment.
  2. Looks for **rejection language** in the replies: "not a bug", "intentional",
     "by design", "works as expected", "wontfix". Such a phrase from a maintainer
     means our finding was false, and the reason is named right there.
  3. Writes a row into the review log: pull request, outcome, quote, link.
  4. Turns confirmed lessons into suppression rules that the refuter reads
     (`refute.py --lessons`). From that point the same class of finding no
     longer reaches submission.

WHAT THE TOOL DOES NOT DO, and this part matters:

  - **it does not decide what a lesson means.** It brings the maintainer phrase
    and the coordinate; the wording of the rule is written by a human. A rule
    derived automatically from someone else's remark is the invented rule that
    once produced 14,022 false findings;
  - **it answers no maintainers and sends nothing anywhere.** Any text a live
    person will read is sent by the human author;
  - **it does not count silence as rejection.** A pull request with no reply is
    absence of data. Of 48 submissions, 41 had been open for less than a day.

Run:
  python3 lessons.py --author karpovantonme
  python3 lessons.py --offline            # cache only
  python3 lessons.py --rules rules.json     # export rules for the refuter

Tests: test_lessons.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field as dc_field
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402
from liftdrift import GitHub, DEFAULT_CACHE  # noqa: E402

LESSONS_TSV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "review-lessons.tsv"
)

# Rejection language: the maintainer says the finding does not exist.
REJECTION = re.compile(
    r"\b(?:not a bug|isn'?t a bug|no[t]? an? issue|by design|intentional(?:ly)?|"
    r"works? as (?:expected|intended|designed)|wont ?fix|won'?t fix|"
    r"this is expected|expected behaviou?r|false positive|not applicable|"
    r"we do this on purpose|deliberate)\b",
    re.I,
)
# A rework request is no rejection, but still a lesson: what we failed to bring.
#
# The bare word "changelog" is deliberately absent here. On a live run it caught
# two MontePy replies where the maintainers said the OPPOSITE: "the changelog
# test failure can be ignored". A word with no request around it means nothing,
# the same species as a flag name with no context.
REWORK = re.compile(
    r"\b(?:could you|would you mind|"
    r"can you (?:also |please )?(?:add|update|fix|change|remove|rebase|squash|sign[- ]?off)|"
    r"please (?:add|update|fix|change|remove|rebase|squash|sign[- ]?off)|"
    r"needs? (?:a |an )?(?:test|changelog|rebase|sign[- ]?off)|"
    r"(?:add|update) (?:a |an |the )?(?:test|changelog|entry)|"
    r"missing (?:a |an )?(?:test|changelog|sign[- ]?off))\b",
    re.I,
)
# A negation before a request cancels it: "no need to add a test".
NEGATED = re.compile(
    r"(?:no need|don'?t|do not|not (?:required|needed|necessary)|"
    r"can be ignored|we can ignore|ignore the|skip the)\s*(?:\w+\s+){0,4}$",
    re.I,
)
# Bots write templates and commands rather than opinions. Their reply is no lesson.
BOTS = frozenset({
    "all-contributors", "codecov", "dependabot", "github-actions", "netlify",
    "sonarcloud", "pre-commit-ci", "readthedocs", "claassistant", "stale",
    "linux-foundation-easycla", "vercel", "changeset-bot", "mergify",
})


@dataclass
class Lesson:
    repo: str
    number: int
    url: str
    state: str
    author: str
    kind: str          # rejection | rework | merge
    quote: str
    date: str


@dataclass
class Report:
    prs_seen: int = 0
    prs_with_replies: int = 0
    silent: List[str] = dc_field(default_factory=list)
    lessons: List[Lesson] = dc_field(default_factory=list)
    api_calls: int = 0
    cache_hits: int = 0


# --------------------------------------------------------------------------


def collect(gh: GitHub, author: str, limit: int = 200) -> List[dict]:
    q = (
        f"search/issues?q=is:pr+author:{author}&per_page={min(limit, 100)}"
        "&sort=updated&order=desc"
    )
    try:
        data = gh.api(q, paginate=True)
    except LookupError:
        # offline with an empty cache is absence of data rather than a breakage
        return []
    if isinstance(data, dict):
        return data.get("items", []) or []
    out: List[dict] = []
    for chunk in data if isinstance(data, list) else []:
        if isinstance(chunk, dict):
            out.extend(chunk.get("items", []) or [])
        elif isinstance(chunk, list):
            out.extend(chunk)
    return out


def comments_of(gh: GitHub, repo: str, number: int) -> List[dict]:
    out: List[dict] = []
    for path in (
        f"repos/{repo}/issues/{number}/comments?per_page=100",
        f"repos/{repo}/pulls/{number}/comments?per_page=100",
        f"repos/{repo}/pulls/{number}/reviews?per_page=100",
    ):
        try:
            d = gh.api(path)
        except LookupError:
            continue
        if isinstance(d, list):
            out.extend(x for x in d if isinstance(x, dict))
    return out


def is_bot(user: dict) -> bool:
    """Whether the author of a reply is a bot.

    On a live run a checklist template from `github-actions[bot]` in nilearn was
    taken for a maintainer asking for rework. A bot holds no opinion about a
    finding.
    """
    login = (user or {}).get("login", "")
    if (user or {}).get("type", "") == "Bot":
        return True
    return login.endswith("[bot]") or login.lower().rstrip("[bot]").strip("-") in BOTS


def directed_elsewhere(body: str, me: str) -> bool:
    """The reply opens by addressing somebody other than us.

    "@all-contributors please add @author for code" is a command to a bot and a
    thank-you in substance rather than a rework request. A reply addressed to us
    ("@author could you add a test") stays a lesson.
    """
    m = re.match(r"\s*@([\w.-]+)", body)
    return bool(m) and m.group(1).lower() != me.lower()


def classify(body: str, me: str = "") -> Optional[str]:
    if REJECTION.search(body):
        return "rejection"
    m = REWORK.search(body)
    if m:
        if me and directed_elsewhere(body, me):
            return None
        if NEGATED.search(body[max(0, m.start() - 60) : m.start()]):
            return None
        return "rework"
    return None


def quote_of(body: str, rx: re.Pattern) -> str:
    m = rx.search(body)
    if not m:
        return body.strip().splitlines()[0][:160] if body.strip() else ""
    start = max(0, m.start() - 90)
    return " ".join(body[start : m.end() + 110].split())


def analyse(gh: GitHub, author: str, report: Report, limit: int = 200) -> None:
    seen: set = set()
    for pr in collect(gh, author, limit):
        url = pr.get("html_url", "")
        m = re.match(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url)
        if not m:
            continue
        repo, number = m.group(1), int(m.group(2))
        report.prs_seen += 1
        state = "merged" if (pr.get("pull_request") or {}).get("merged_at") else pr.get("state", "?")

        replies = [
            c for c in comments_of(gh, repo, number)
            if (c.get("user") or {}).get("login", "").lower() != author.lower()
            and (c.get("body") or "").strip()
            and not is_bot(c.get("user") or {})
        ]
        if not replies:
            report.silent.append(f"{repo}#{number}: no reply, no lesson")
            continue
        report.prs_with_replies += 1

        for c in replies:
            body = c.get("body") or ""
            kind = classify(body, author)
            if not kind:
                continue
            rx = REJECTION if kind == "rejection" else REWORK
            quote = quote_of(body, rx)
            key = (repo, number, quote[:60])
            if key in seen:
                continue      # the same reply arrives as a review and as a comment
            seen.add(key)
            report.lessons.append(
                Lesson(
                    repo=repo, number=number, url=url, state=state,
                    author=(c.get("user") or {}).get("login", "?"),
                    kind=kind, quote=quote,
                    date=(c.get("created_at") or c.get("submitted_at") or "")[:10],
                )
            )
    report.api_calls = gh.calls
    report.cache_hits = gh.cache_hits


def _cell(text: str) -> str:
    """A TSV column does not survive tabs or newlines inside a value."""
    return " ".join(str(text).split())


def write_tsv(report: Report, path: str = LESSONS_TSV) -> None:
    new = not os.path.exists(path)
    seen = set()
    if not new:
        for ln in common.read_text(path).splitlines()[1:]:
            f = ln.split("\t")
            if len(f) > 5:
                seen.add((f[1], f[2], f[5][:60]))
    with open(path, "a", encoding="utf-8") as fh:
        if new:
            fh.write("date\trepo\tpr\tstate\tkind\tquote\tmaintainer\turl\trule\n")
        for l in report.lessons:
            key = (l.repo, str(l.number), l.quote[:60])
            if key in seen:
                continue
            seen.add(key)
            fh.write(
                f"{_cell(l.date)}\t{_cell(l.repo)}\t{l.number}\t{_cell(l.state)}\t"
                f"{_cell(l.kind)}\t{_cell(l.quote)}\t{_cell(l.author)}\t{_cell(l.url)}\t\n"
            )


def rules_from_tsv(path: str = LESSONS_TSV) -> List[dict]:
    """Suppression rules from the LAST column, the one a human fills in.

    Deriving a rule from the quote automatically is deliberately not done: a rule
    invented from someone else's remark is the kind that once produced 14,022
    false findings.
    """
    out: List[dict] = []
    text = common.read_text(path)
    for ln in text.splitlines()[1:]:
        f = ln.split("\t")
        if len(f) >= 9 and f[8].strip():
            out.append({"pattern": f[8].strip(), "why": f[5][:120], "source": f[7]})
    return out


# --------------------------------------------------------------------------


def print_report(report: Report, verbose: bool = False) -> None:
    rejections = [l for l in report.lessons if l.kind == "rejection"]
    reworks = [l for l in report.lessons if l.kind == "rework"]

    for title, items in (("Rejections: the finding was false", rejections), ("Rework requested", reworks)):
        if not items:
            continue
        print(f"\n=== {title} ({len(items)}) ===")
        for l in items:
            print(f"\n  {l.repo}#{l.number} ({l.state}), {l.author}, {l.date}")
            print(f"    «{l.quote}»")
            print(f"    {l.url}")

    print("\n=== Coverage ===")
    if report.prs_seen == 0:
        print("  no pull requests seen: no network and an empty cache")
    print(f"  pull requests seen:     {report.prs_seen}")
    print(f"  with a maintainer reply:{report.prs_with_replies}")
    print(f"  silent:                 {len(report.silent)} (absence of data, no lesson)")
    print(f"  GitHub requests:        {report.api_calls} (from cache {report.cache_hits})")
    print(common.findings_line(len(rejections), len(reworks)))
    print(stamp.line(__file__, ["common.py"]))

    if verbose and report.silent:
        print(f"\n--- Silent ({len(report.silent)}) ---")
        for s in report.silent[:40]:
            print(f"  {s}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Harvest lessons from review replies")
    ap.add_argument("--author", default="karpovantonme")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    ap.add_argument("--tsv", default=LESSONS_TSV)
    ap.add_argument("--rules", help="export suppression rules for the refuter")
    ap.add_argument("--no-write", action="store_true")
    common.add_common_args(ap, network=True)
    args = ap.parse_args(argv)

    gh = GitHub(args.cache, args.offline)
    report = Report()
    analyse(gh, args.author, report, args.limit)
    print_report(report, args.verbose)

    if not args.no_write:
        write_tsv(report, args.tsv)
        print(f"  appended to:            {os.path.basename(args.tsv)}")
    if args.rules:
        with open(args.rules, "w", encoding="utf-8") as fh:
            json.dump(rules_from_tsv(args.tsv), fh, ensure_ascii=False, indent=1)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [
                    {
                        "hard": l.kind == "rejection", "repo": l.repo, "number": l.number,
                        "state": l.state, "kind": l.kind, "quote": l.quote,
                        "author": l.author, "url": l.url, "date": l.date,
                    }
                    for l in report.lessons
                ],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if any(l.kind == "rejection" for l in report.lessons) else 0


if __name__ == "__main__":
    sys.exit(main())
