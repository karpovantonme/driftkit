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

THE RULES ARE NOT ALWAYS IN THE REPOSITORY. Astropy keeps its policy on AI
contributions in a sibling repository of the organisation, astropy-project, and
reading the clone alone said the door was open. Two patches were closed there.
GitHub itself spreads a `.github` repository across the whole organisation, so
this is not one project's quirk. Of the 97 owners in the pool, 29 keep such a
repository, 6 keep `community`, 4 keep `governance`, and three organisations
hold a document about AI outside the project (`--offline` skips this and says
so).

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
    org_checked: List[str] = dc_field(default_factory=list)
    org_files: List[str] = dc_field(default_factory=list)
    org_error: str = ""
    mirror: Optional[str] = None


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
        if self.rules.mirror:
            stops.append(f"a mirror, pull requests go elsewhere: {self.rules.mirror}")
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
        return bool(self.rules.ai_policy_file or self.rules.issue_required
                    or self.rules.mirror)


# --------------------------------------------------------------------------


def slug_of(root: str) -> Optional[str]:
    for cmd in (["git", "-C", root, "config", "--get", "remote.origin.url"],):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            m = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?\s*$", r.stdout.strip())
            if m:
                return m.group(1)
    return None


def scan_text(rules: Rules, label: str, text: str) -> None:
    """One rule document, wherever it came from: the clone or the organisation.

    `label` goes into every finding, so a report always says which file said it
    and, for an organisation, which repository the file was in.
    """
    if not rules.ai_policy_file:
        m = AI_SECTION.search(text)
        if m and ai_section_is_strict(text, m.end()):
            rules.ai_policy_file = (
                f"{label}:{common.line_of(text, m.group(0).strip())} "
                f"(section '{m.group(0).strip().lstrip('# ')[:40]}', with requirements)"
            )
        elif m:
            rules.ai_section_soft = f"{label}: section '{m.group(0).strip().lstrip('# ')[:40]}' with no requirements"
    if AI_MENTION.search(text):
        rules.ai_mentioned_in.append(label)
    if not rules.issue_required and ISSUE_REQUIRED.search(text):
        m = ISSUE_REQUIRED.search(text)
        rules.issue_required = f"{label}:{common.line_of(text, m.group(0))}"
    if not rules.agent_trap and AGENT_TRAP.search(text):
        m = AGENT_TRAP.search(text)
        rules.agent_trap = f"{label}:{common.line_of(text, m.group(0))}"
    if not rules.disclosure and AI_DISCLOSURE.search(text):
        m = AI_DISCLOSURE.search(text)
        rules.disclosure = f"{label}:{common.line_of(text, m.group(0))}"
    if DCO.search(text):
        rules.dco = True
    if CLA.search(text):
        rules.cla = True


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
        scan_text(rules, rel, text)
    return rules


# --------------------------------------------------------------------------
# The rules of the organisation, one level above the project

# Names measured across the 97 owners of the pool on 07.08, not guessed. The
# two templated ones are the astropy case: an organisation names its meta
# repository after itself.
ORG_META_REPOS = (".github", "community", "governance", "{owner}-project", "{owner}-policies")

# Where in such a repository the rules lie. A closed list on purpose: milvus
# keeps `blog/en/building-a-production-ready-ai-assistant-...md` and four more
# like it, and a search by name alone reads a blog post as a policy.
ORG_LOOK_IN = frozenset({"", "doc", "docs", "policies", "policy", ".github", "profile"})

_ORG_AI_DOC = re.compile(
    r"^[^/]*\b(?:ai|llm)[-_ ]?(?:polic|usage|guidel|assist|generat)[^/]*\.(?:md|rst|txt)$", re.I
)
_ORG_RULE_DOC = re.compile(
    r"^(?:CONTRIBUTING|AGENTS|CLAUDE|GOVERNANCE)\.(?:md|rst|txt)$|"
    r"^(?:pull_request_template|PULL_REQUEST_TEMPLATE)\.(?:md|txt)$",
    re.I,
)
_API = "https://api.github.com"
UA = "driftkit-sitecheck/1.0"
_token_cache: List[Optional[str]] = []


def api_token() -> Optional[str]:
    """A token lifts the anonymous limit of 60 requests an hour to 5000.

    Works without one; five requests per project means twelve projects an hour.
    """
    if _token_cache:
        return _token_cache[0]
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            tok = r.stdout.strip()
    _token_cache.append(tok or None)
    return _token_cache[0]


def api_json(path: str, timeout: float = 20.0):
    """One GitHub API call. A separate function so a test can replace it."""
    import urllib.error
    import urllib.request

    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    tok = api_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(f"{_API}/{path.lstrip('/')}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_error": f"http {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"net:{type(e).__name__}"}


def _worth_reading(path: str) -> bool:
    head, _, name = path.rpartition("/")
    if head not in ORG_LOOK_IN:
        return False
    return bool(_ORG_AI_DOC.match(name) or _ORG_RULE_DOC.match(name))


_org_cache: Dict[str, tuple] = {}


def org_rule_texts(owner: str) -> tuple:
    """Rule documents of the organisation: {"owner/repo:path": text}, repos read, error.

    One tree request per candidate repository; the ones that do not exist answer
    404 and cost nothing. A truncated tree is reported rather than passed off as
    "nothing found" -- those are different statements.
    """
    if owner in _org_cache:   # a sweep holds several projects of one owner
        return _org_cache[owner]
    texts: Dict[str, str] = {}
    read: List[str] = []
    problems: List[str] = []
    for tmpl in ORG_META_REPOS:
        repo = tmpl.format(owner=owner)
        slug = f"{owner}/{repo}"
        tree = api_json(f"repos/{slug}/git/trees/HEAD?recursive=1")
        if not isinstance(tree, dict) or "_error" in tree:
            err = (tree or {}).get("_error", "no answer")
            if not err.startswith("http 404"):
                problems.append(f"{slug}: {err}")
            continue
        read.append(slug + (" (tree truncated)" if tree.get("truncated") else ""))
        for item in tree.get("tree", []):
            if item.get("type") != "blob" or not _worth_reading(item["path"]):
                continue
            blob = api_json(f"repos/{slug}/git/blobs/{item['sha']}")
            if not isinstance(blob, dict) or "_error" in blob:
                problems.append(f"{slug}:{item['path']}: unreadable")
                continue
            import base64
            try:
                body = base64.b64decode(blob.get("content", "")).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                continue
            texts[f"{slug}:{item['path']}"] = body
    out = (texts, read, "; ".join(problems))
    # A failed read is never cached: a network blink would otherwise pass for
    # "the organisation is clean" for the rest of the sweep.
    if not problems:
        _org_cache[owner] = out
    return out


# A repository that only mirrors another forge takes no pull requests at all,
# and no amount of reading its rules will say so: the rules it carries are the
# rules of the real home. GNOME states it in the description of every mirror,
# "Read-only mirror of https://gitlab.gnome.org/GNOME/glib". Found the hard way
# on 07.08 after a full sweep of glib, pango, libsoup and json-glib, all four
# of which live on gitlab.gnome.org.
MIRROR = re.compile(
    r"read[- ]only mirror|mirror of\s+http|"
    r"official (?:git )?repository (?:is|lives) at|"
    r"development (?:happens|takes place) (?:on|at)\s+http|"
    r"do not (?:send|open) pull requests here",
    re.I,
)


def read_mirror(rules: Rules, slug: str) -> None:
    """Whether this repository is a mirror of a home somewhere else."""
    meta = api_json(f"repos/{slug}")
    if not isinstance(meta, dict) or "_error" in meta:
        return
    text = " ".join(str(meta.get(k) or "") for k in ("description", "homepage"))
    m = MIRROR.search(text)
    if m:
        rules.mirror = text.strip()[:160]
    elif meta.get("archived"):
        rules.mirror = "archived: the repository is read-only"


def read_org_rules(rules: Rules, slug: str) -> None:
    """Adds the rules of the organisation to what the clone already said."""
    owner = slug.split("/")[0]
    texts, read, err = org_rule_texts(owner)
    rules.org_checked = read
    rules.org_error = err
    # Which files were actually opened. Without this "the organisation says
    # nothing" is indistinguishable from "not a single file was read", and that
    # confusion has already cost this kit twice.
    rules.org_files = list(texts)
    for label, text in texts.items():
        name = label.rpartition("/")[2]
        if not rules.ai_policy_file and _ORG_AI_DOC.match(name):
            # A dedicated document, the same as one inside the project
            rules.ai_policy_file = label
            continue
        scan_text(rules, label, text)


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
    # The organisation is asked before liveness: its rules can close the door
    # the clone left open, and then there is nothing to measure.
    if offline:
        site.rules.org_error = "network disabled, the organisation was not read"
    elif not site.slug:
        site.rules.org_error = "no repository resolved, the organisation was not read"
    else:
        read_mirror(site.rules, site.slug)
        if not site.rules.ai_policy_file or not site.rules.issue_required:
            read_org_rules(site.rules, site.slug)
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
    if r.org_checked:
        print(f"  organisation read:      {', '.join(r.org_checked)}")
        print(f"  its rule files read:    {len(r.org_files)}"
              + (f" ({', '.join(r.org_files)})" if verbose and r.org_files else ""))
    if r.org_error:
        print(f"  organisation:           {r.org_error}")
    if r.mirror:
        print(f"  a mirror:               {r.mirror}")
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
                        "org_checked": site.rules.org_checked,
                        "org_files": site.rules.org_files,
                        "org_error": site.rules.org_error,
                        "mirror": site.rules.mirror,
                    },
                    "score": site.score,
                }],
                fh, ensure_ascii=False, indent=1,
            )
    return 1 if site.blocked else 0   # a hard finding means we stay away


if __name__ == "__main__":
    sys.exit(main())
