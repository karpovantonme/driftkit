#!/usr/bin/env python3
"""sitecheck.py: is this project worth checking at all.

The first stage of the pipeline. Before it, the sweep runner answered only "what
can be checked here", which is half the job. A finding on a dead project, or on
a project that does not want the contribution, is time spent for nothing.

Two independent questions, both asked BEFORE any detector runs:

  1. **Can we bring anything here at all.** The project rules get read. A
     dedicated document about AI contributions means we walk past: once a
     project writes such a document it treats the matter strictly, and pushing
     against that mood is pointless. A mandatory pre-approved issue (argo-cd,
     jaeger) is a stop for an ordinary approach too.
  2. **Will a patch reach a merge.** Measured from the merge log: how many
     distinct people merge, how many open pull requests per merger, whether
     newcomers get in.

THE DISTINCTION THAT MATTERS. "Zero newcomers" means two different things.
There is a queue and no newcomers get in: the door is closed. There is no queue
and no newcomers: the door has simply never been pushed, and that is exactly
where it is worth going. The signal is other people's open pull requests rather
than the newcomer count.

WHAT THE TOOL DOES NOT DO:
  - it does not decide for a human. It prints a verdict and a reason; whether to
    go is decided elsewhere;
  - it does not count bots as people. The bot mask is knowingly incomplete, see
    the `meeseeksmachine` case;
  - without the network it gives the rule analysis only and marks liveness
    honestly as not measured.

Run:
  python3 sitecheck.py --dir ~/Projects/oss/etcd
  python3 sitecheck.py --dir ... --offline    # rules only, no network

Tests: test_sitecheck.py next to this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import stamp  # noqa: E402

FLOW_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")

# A dedicated document about AI contributions: we walk past
AI_POLICY_FILES = (
    "ai_policy.md", "ai-policy.md", "ai_usage.md", "ai-usage-guidelines.md",
    "policies/ai-policy.md", "doc/dev/ai-usage-guidelines.md",
)
RULE_FILES = (
    "CONTRIBUTING.md", "CONTRIBUTING.rst", "AGENTS.md", "CLAUDE.md",
    ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md",
)
TEMPLATE_FILES = (
    ".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md", "docs/pull_request_template.md",
)

# A mandatory pre-approved issue stops an ordinary approach
ISSUE_REQUIRED = re.compile(
    r"do\s*not\s+(?:open|create|submit|raise)\s+an?\s+(?:pr|pull\s*request)[^.]{0,60}unless|"
    r"only through an? (?:approved )?issue|require an issue|"
    r"all work .{0,20} tracked through issues|require[sd]? an? (?:existing|approved|open) issue|"
    r"must be linked to an issue|issue (?:is )?(?:required|mandatory)",
    re.I,
)

# A dedicated SECTION about AI counts the same as a dedicated file. In qdrant it
# is exactly a section inside docs/CONTRIBUTING.md, and without this check a
# project we had closed ourselves read as open. Keep it apart from a one-liner
# "AI is fine, you answer for the result", which is an ordinary rule rather than
# strictness (Astropy, Keycloak, ClickHouse).
AI_SECTION = re.compile(
    r"^\s{0,3}#{1,6}\s*.*\b(?:ai|llm|artificial intelligence|generative)\b.*$",
    re.I | re.M,
)

# The mere presence of an AI section means nothing. In rclone the "AI-assisted
# contributions" section opens with "You are welcome to use AI coding
# assistants", and Nick Craig-Wood merged our patch personally. What makes a
# section strict is a REQUIREMENT: mark the commits, disclose the prompt, write
# the description by hand, do not generate sources. Without that distinction the
# tool closed a project where we already had a merge.
AI_STRICT = re.compile(
    r"\b(?:mark|label|tag)\s+(?:your\s+)?commits|disclose|"
    r"share\s+the\s+prompt|original\s+prompt|"
    r"(?:must|should|do)\s*not\s+(?:be\s+)?(?:generate|use\s+ai|written by)|"
    r"written by a human|not\s+(?:be\s+)?generated|"
    r"\[AI\]|\[manual\]|prohibited|forbidden|not\s+allowed",
    re.I,
)
AI_WELCOME = re.compile(
    r"you are welcome to use|we recognize the usefulness|feel free to use|"
    r"do not have to disclose|no need to disclose", re.I
)


def ai_section_is_strict(text: str, at: int) -> bool:
    """Whether the AI section is strict. The body decides, not the heading."""
    body = text[at : at + 2500]
    if AI_WELCOME.search(body) and not AI_STRICT.search(body):
        return False
    return bool(AI_STRICT.search(body))
# An agent trap: a request only someone who read the template can satisfy
AGENT_TRAP = re.compile(
    r"if you are an ai|if you'?re an ai|ai agent, please|write a rhyme|"
    r"funny cat joke|share the prompt", re.I
)
AI_MENTION = re.compile(r"\b(?:ai|llm|copilot|chatgpt|claude|generative)\b", re.I)

# A REAL rule almost always sits next to the trap. In etcd one template comment
# holds both: "disclose your use of AI" is a rule and a human decides on it;
# "write a poem" is a trap and gets ignored. Merging the two and ignoring both
# would be a violation, doing both would be silly.
AI_DISCLOSURE = re.compile(
    r"(?:please\s+)?disclose\s+(?:this|your|the)?\s*(?:use|usage)?|"
    r"if you used ai tools|declare (?:the )?use of ai", re.I
)
DCO = re.compile(r"\bDCO\b|signed-off-by|git commit -s", re.I)
CLA = re.compile(r"\bCLA\b|contributor license agreement|easycla", re.I)


@dataclass
class Rules:
    ai_policy_file: Optional[str] = None
    ai_mentioned_in: List[str] = dc_field(default_factory=list)
    issue_required: Optional[str] = None
    agent_trap: Optional[str] = None
    ai_section_soft: Optional[str] = None
    disclosure: Optional[str] = None
    dco: bool = False
    cla: bool = False
    checked: List[str] = dc_field(default_factory=list)


@dataclass
class Site:
    path: str
    slug: Optional[str] = None
    rules: Rules = dc_field(default_factory=Rules)
    score: Optional[dict] = None
    score_error: str = ""

    @property
    def verdict(self) -> str:
        stops = []
        if self.rules.issue_required:
            stops.append("pre-approved issue required")
        if self.rules.ai_policy_file:
            stops.append("walk past: AI contribution rules with requirements")
        if stops:
            return "; ".join(stops)
        if self.score is None:
            return "rules allow it; liveness not measured"
        if self.score.get("note") == "asleep":
            return "asleep: no merges in 180 days"
        kk = self.score.get("kk")
        if kk is None:
            return "rules allow it; score not computed"
        if kk >= 0.5:
            return f"lively, score {kk:.2f}"
        if kk >= 0.25:
            return f"passable, score {kk:.2f}"
        return f"tight, score {kk:.2f}, bring only the indisputable"

    @property
    def blocked(self) -> bool:
        return bool(self.rules.ai_policy_file or self.rules.issue_required)


# --------------------------------------------------------------------------


def slug_of(root: str) -> Optional[str]:
    for cmd in (["git", "-C", root, "config", "--get", "remote.origin.url"],):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            m = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?\s*$", r.stdout.strip())
            if m:
                return m.group(1)
    return None


def read_rules(root: str) -> Rules:
    rules = Rules()
    for rel in AI_POLICY_FILES:
        for cand in (rel, rel.upper(), os.path.join("docs", rel), os.path.join(".github", rel)):
            p = os.path.join(root, cand)
            if os.path.isfile(p):
                rules.ai_policy_file = cand
                return rules

    for rel in RULE_FILES + TEMPLATE_FILES:
        p = os.path.join(root, rel)
        if not os.path.isfile(p):
            continue
        text = common.read_text(p)
        if not text:
            continue
        rules.checked.append(rel)
        if not rules.ai_policy_file:
            m = AI_SECTION.search(text)
            if m and ai_section_is_strict(text, m.end()):
                rules.ai_policy_file = (
                    f"{rel}:{common.line_of(text, m.group(0).strip())} "
                    f"(section '{m.group(0).strip().lstrip('# ')[:40]}', with requirements)"
                )
            elif m:
                rules.ai_section_soft = f"{rel}: section '{m.group(0).strip().lstrip('# ')[:40]}' with no requirements"
        if AI_MENTION.search(text):
            rules.ai_mentioned_in.append(rel)
        if not rules.issue_required and ISSUE_REQUIRED.search(text):
            m = ISSUE_REQUIRED.search(text)
            rules.issue_required = f"{rel}:{common.line_of(text, m.group(0))}"
        if not rules.agent_trap and AGENT_TRAP.search(text):
            m = AGENT_TRAP.search(text)
            rules.agent_trap = f"{rel}:{common.line_of(text, m.group(0))}"
        if not rules.disclosure and AI_DISCLOSURE.search(text):
            m = AI_DISCLOSURE.search(text)
            rules.disclosure = f"{rel}:{common.line_of(text, m.group(0))}"
        if DCO.search(text):
            rules.dco = True
        if CLA.search(text):
            rules.cla = True
    return rules


def liveness_module() -> Optional[str]:
    """Path of the liveness scorer, when one is installed next to the kit.

    The rule analysis stands on its own. Measuring how lively a project is needs
    a merge-log scorer, which lives outside this kit, so a report has to tell
    "the module is absent" apart from "the measurement failed". Those are
    different statements and only one of them is about the project.
    """
    path = os.path.join(FLOW_TOOLS, "rescore_people.py")
    return path if os.path.isfile(path) else None


def measure(slug: str) -> Optional[dict]:
    """Project liveness, computed by the scorer when it is installed."""
    path = liveness_module()
    if path is None:
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("rescore", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod.one(slug)
    except Exception:  # noqa: BLE001
        return None


def check(root: str, offline: bool = False) -> Site:
    site = Site(path=os.path.abspath(root), rules=read_rules(root), slug=slug_of(root))
    if offline or not site.slug or site.blocked:
        if site.blocked:
            site.score_error = "rules block the approach, no point measuring liveness"
        elif not site.slug:
            site.score_error = "could not resolve the repository from the git remote"
        else:
            site.score_error = "network disabled"
        return site
    if liveness_module() is None:
        site.score_error = "the liveness scorer is not installed next to the kit"
        return site
    site.score = measure(site.slug)
    if site.score is None:
        site.score_error = "measurement failed"
    return site


# --------------------------------------------------------------------------


def print_report(site: Site, verbose: bool = False) -> None:
    print(f"\n=== Project: {site.slug or os.path.basename(site.path)} ===")
    print(f"  verdict:                {site.verdict}")
    r = site.rules
    print(f"  rule files read:        {len(r.checked)}" + (f" ({', '.join(r.checked)})" if verbose else ""))
    if r.ai_policy_file:
        print(f"  AI policy:              {r.ai_policy_file}, we walk past")
    elif r.ai_section_soft:
        print(f"  AI section is soft:     {r.ai_section_soft}, read it before submitting")
    elif r.ai_mentioned_in:
        print(f"  AI mentioned in:        {', '.join(r.ai_mentioned_in)}, read before submitting")
    if r.issue_required:
        print(f"  issue required first:   {r.issue_required}")
    if r.disclosure:
        print(f"  AI disclosure:          {r.disclosure}, a RULE, the human author decides")
    if r.agent_trap:
        print(f"  trap in the template:   {r.agent_trap}, a suggestion, neither followed nor mentioned")
    if r.dco:
        print("  DCO:                    git commit -s required")
    if r.cla:
        print("  CLA:                    mentioned, check the signature")
    if site.score:
        s = site.score
        print(f"  outside people, 180d:   {s.get('npeople', '?')}")
        print(f"  open pull requests:     {s.get('openpr', '?')}")
        print(f"  median wait, hours:     {s.get('wait') and round(s['wait'], 1)}")
    elif site.score_error:
        print(f"  liveness:               not measured ({site.score_error})")

    print("\n=== Coverage ===")
    print(f"  rule files read:        {len(r.checked):>3}")
    print(f"  reasons to stay away:   {len(site.reasons) if hasattr(site, 'reasons') else (1 if site.blocked else 0)}")
    # There is exactly one hard finding here and it means "do not bring anything".
    print(common.findings_line(1 if site.blocked else 0, 0 if site.blocked else 1))
    print(stamp.line(__file__, ["common.py"]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Is this project worth checking")
    ap.add_argument("--dir", required=True)
    common.add_common_args(ap, network=True)
    args = ap.parse_args(argv)

    site = check(args.dir, args.offline)
    print_report(site, args.verbose)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                [{
                    "hard": site.blocked,
                    "path": site.path, "slug": site.slug, "verdict": site.verdict,
                    "blocked": site.blocked,
                    "rules": {
                        "ai_policy_file": site.rules.ai_policy_file,
                        "ai_mentioned_in": site.rules.ai_mentioned_in,
                        "issue_required": site.rules.issue_required,
                        "agent_trap": site.rules.agent_trap,
                        "disclosure": site.rules.disclosure,
                        "dco": site.rules.dco, "cla": site.rules.cla,
                        "checked": site.rules.checked,
                    },
                    "score": site.score,
                }],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if site.blocked else 0   # a hard finding means we stay away


if __name__ == "__main__":
    sys.exit(main())
