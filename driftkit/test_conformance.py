#!/usr/bin/env python3
"""test_conformance.py: the kit has to be one thing, not eleven separate ones.

Written after comparing the tools against each other turned up a real bug:
`liftdrift` called its confidence flag `confident` in JSON while everyone else
called it `hard`. The sweep runner read `hard` defaulting to True and
**counted soft findings as hard**. No test of liftdrift itself could have
caught it: internally the tool was perfectly consistent.

Hence this file. The contract lives in `common.py`; here it is enforced.
A contract nobody checks drifts apart within a week.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import common  # noqa: E402

# Detectors: they look for defects in someone else's code. Their exit code is
# tied to findings.
DETECTORS = [
    "ifacedrift", "liftdrift", "transdrift", "gitdrift",
    "supportdrift", "namedrift", "deaddrift", "assertdrift", "linkdrift",
    "docdrift", "doxdrift",
]
# Pipeline stages: they do not look for defects, they decide what to do with them.
PIPELINE = ["sitecheck", "refute", "probe", "lessons", "buildprobe"]

# Two detectors may live one directory up, where they were originally written.
# The contract covers them too: they ship in the same repository as the rest.
PARENT = os.path.dirname(HERE)


def path_of(tool: str) -> str:
    here = os.path.join(HERE, tool + ".py")
    return here if os.path.exists(here) else os.path.join(PARENT, tool + ".py")


DETECTORS = [t for t in DETECTORS if os.path.exists(path_of(t))]
PIPELINE = [t for t in PIPELINE if os.path.exists(path_of(t))]
TOOLS = DETECTORS + PIPELINE

NETWORK_TOOLS = {"liftdrift", "transdrift", "gitdrift", "linkdrift", "lessons", "sitecheck"}

# The build prober is the only tool that never has hard findings: it answers
# "is this project usable" rather than "is there a defect here". Its exit code
# is therefore always 0, and its header says so.
NO_HARD_FINDINGS = {"buildprobe"}

# The refuter creates no findings of its own, it passes other tools' findings
# through. The hard flag in its output is the one the original finding carried,
# and it must not be touched. A separate test below watches exactly that.
PASSTHROUGH = {"refute"}


def source(tool: str) -> str:
    return common.read_text(path_of(tool))


def helptext(tool: str) -> str:
    p = subprocess.run(
        [sys.executable, path_of(tool), "--help"],
        capture_output=True, text=True,
    )
    return p.stdout


class TestContract(unittest.TestCase):
    def test_every_tool_is_present(self):
        self.assertGreaterEqual(len(DETECTORS), 9, f"detectors found: {len(DETECTORS)}")
        self.assertGreaterEqual(len(PIPELINE), 5, f"pipeline stages found: {len(PIPELINE)}")

    def test_common_keys(self):
        for t in TOOLS:
            h = helptext(t)
            self.assertIn("--json", h, t)
            self.assertIn("--verbose", h, t)

    def test_network_tools_have_offline_switch(self):
        """"Stay off the network" was --offline in one place and --no-proof in two
        others. For the kit it is one flag."""
        for t in TOOLS:
            if t not in NETWORK_TOOLS:
                continue
            self.assertIn("--offline", helptext(t), f"{t}: missing the shared --offline flag")

    def test_json_findings_carry_hard(self):
        """The real bug was exactly here: sweep reads `hard` while liftdrift wrote
        `confident`, so soft findings were counted as hard."""
        for t in TOOLS:
            if t in PASSTHROUGH:
                continue
            s = source(t)
            m = re.search(r"json\.dump\(\s*\[(.*?)\]\s*,", s, re.S)
            self.assertIsNotNone(m, f"{t}: could not find the JSON dump of a findings list")
            # `"hard": ...` in a dict or `hard=is_hard(h)` in a constructor:
            # both forms put the very key the contract talks about into JSON.
            self.assertTrue(
                '"hard"' in m.group(1) or re.search(r"\bhard\s*=", m.group(1)),
                f"{t}: findings in JSON carry no hard flag",
            )

    def test_passthrough_preserves_hard(self):
        """The refuter must return a finding with the same hard flag.

        If it drops or rewrites the flag, the sweep runner starts counting soft
        findings as hard again: the very bug this file was written for, coming
        from the other side.
        """
        import json as _j, tempfile as _t, subprocess as _s
        d = _t.mkdtemp()
        src = os.path.join(d, "in.json")
        dst = os.path.join(d, "out.json")
        _j.dump([{"hard": False, "file": "no-such-file.go", "line": 1,
                  "kind": "contract check"}], open(src, "w", encoding="utf-8"))
        _s.run([sys.executable, os.path.join(HERE, "refute.py"),
                "--findings", src, "--json", dst], capture_output=True, text=True)
        if os.path.exists(dst):
            for x in _j.load(open(dst, encoding="utf-8")):
                self.assertIn("hard", x, "refute dropped the hard flag")
                self.assertFalse(x["hard"], "refute rewrote the hard flag")

    def test_coverage_block_and_findings_line(self):
        for t in TOOLS:
            s = source(t)
            self.assertIn("=== Coverage ===", s, t)
            # Either the line is written out or it comes from the shared helper.
            # The second is better: the format then stays one for the whole kit.
            self.assertTrue(
                re.search(r"findings:\s*\{?[^\"]*hard", s) or "common.findings_line(" in s,
                f"{t}: no 'N hard, M soft' line",
            )

    def test_stamp_is_printed(self):
        """A number without a stamp counts as unverified."""
        for t in TOOLS:
            self.assertIn("stamp.line(", source(t), f"{t}: report has no run stamp")

    def test_exit_code_is_tied_to_hard_findings(self):
        for t in TOOLS:
            if t in NO_HARD_FINDINGS:
                continue
            s = source(t)
            self.assertRegex(
                # Every stage names "hard" differently: a finding for a detector,
                # a block for the site gate, a proven mutation for the prober.
                # Different names, one meaning.
                s, r"return 1 if .*(hard|confident|findings|blocked|proven|survivors|rejection)",
                f"{t}: exit code is not tied to findings"
            )

    def test_docstring_names_the_blind_spots(self):
        """A maintainer was promised tools "with documented blind spots".
        The module header is the only place that promise lives."""
        for t in TOOLS:
            doc = source(t).split('"""')[1] if '"""' in source(t) else ""
            self.assertGreater(len(doc), 400, f"{t}: header is too short")
            self.assertRegex(
                doc.lower(),
                r"(blind spot|does not (see|catch|parse|read|judge)|is not|never |only )",
                f"{t}: header does not say what the tool cannot see",
            )

    def test_no_private_skip_list_inside_a_walk(self):
        """A skip list written inline in `os.walk` escapes the check above.

        `gitdrift` named three directories right inside `dirs[:] = [...]`, so it
        never matched the `SKIP_DIRS =` pattern and quietly walked trees the rest
        of the kit skips. A private list drifts apart; that is the bug this file
        exists for, in a place the file could not see.
        """
        for t in TOOLS + ["sweep"]:
            s = source(t)
            # The whole statement, not the first line of it: a list written
            # across three lines used to slip past this very check.
            for m in re.finditer(r"dirs\[:\]\s*=\s*(.+?\])", s, re.S):
                self.assertTrue(
                    "SKIP_DIRS" in m.group(1) or "common." in m.group(1),
                    f"{t}: a skip list written inline in the walk",
                )

    def test_every_reader_counts_what_it_skips(self):
        """A file dropped without a counter makes the report look cleaner.

        Four tools used to `continue` past an unreadable or oversized file with
        no trace at all. The whole species is about coverage rather than false
        findings, and it costs more: a false finding shouts, a gap stays silent.
        """
        for t in ("deaddrift", "namedrift", "linkdrift", "doxdrift", "gitdrift", "docdrift"):
            if t not in TOOLS:
                continue
            s = source(t)
            self.assertRegex(
                s, r"(files_skipped|skipped_files|COUNTS\[.skipped.\]|unparsable)",
                f"{t}: skipped files are not counted",
            )
            self.assertRegex(
                s, r"(files skipped|headers skipped|failed to parse)",
                f"{t}: skipped files are not printed in the coverage block",
            )

    def test_no_private_skip_lists(self):
        """The skip list was declared three times, differently each time.
        There is one for the whole kit."""
        for t in TOOLS + ["sweep"]:
            s = source(t)
            for m in re.finditer(r"SKIP_DIRS\s*=\s*(.+)", s):
                self.assertIn("common.SKIP_DIRS", m.group(1), f"{t}: private skip list")


class TestEveryToolDeclaresItsPlace(unittest.TestCase):
    """Where a check runs belongs to the contract, not to whoever starts it.

    Three kinds live here: one reads text, one talks to other people's servers,
    one executes foreign code. Only the first is safe to start anywhere. An
    undeclared tool would quietly default to local, which is exactly how a
    laptop ends up compiling somebody else's test suite.
    """

    def test_every_tool_has_a_place(self):
        for t in TOOLS:
            self.assertIn(t, common.PLACE, f"{t}: no place declared in common.PLACE")
            self.assertIn(common.PLACE[t], (common.LOCAL, common.NETWORK, common.BUILD))

    def test_a_build_check_does_not_run_here_by_default(self):
        for t in TOOLS:
            if common.place_of(t) != common.BUILD:
                continue
            self.assertFalse(common.runs_here(t), f"{t}: a build check must not run locally")
            self.assertTrue(common.runs_here(t, allow_build=True))

    def test_the_ceilings_exist_and_are_small(self):
        """A hundred parallel clones is no technical problem, it is bad manners."""
        for key in ("targets_per_run", "parallel_targets", "job_timeout_minutes",
                    "network_runs_at_once", "clone_depth"):
            self.assertIn(key, common.LIMITS)
        self.assertLessEqual(common.LIMITS["parallel_targets"], 4)
        self.assertLessEqual(common.LIMITS["targets_per_run"], 20)
        self.assertEqual(common.LIMITS["network_runs_at_once"], 1)


class TestWorkflowsHonourTheLimits(unittest.TestCase):
    """The ceilings in the kit and the ceilings in CI have to be the same ones.

    Two copies of a number drift apart; that is the bug this whole file exists
    for. Here the risk is worse than a wrong report: a workflow that quietly
    grew its parallelism spends somebody else's runners.
    """

    # The workshop is the source; the published repository is assembled from it.
    # A workflow kept only in the published tree would be the second copy of one
    # thing, which is the bug this file exists for.
    # Two layouts, one source. In the workshop the sources sit next to the kit;
    # in the published repository GitHub requires them at `.github/workflows`.
    # The check reads whichever is there rather than assuming one of them.
    PLACES = (os.path.join(HERE, "workflows"),
              os.path.join(PARENT, ".github", "workflows"))

    def workflow(self, name):
        for base in self.PLACES:
            path = os.path.join(base, name)
            if os.path.isfile(path):
                return common.read_text(path)
        self.fail(f"{name}: no workflow source in either layout")

    @staticmethod
    def _uncommented(text: str) -> str:
        """Only what YAML actually reads.

        The first version of this check searched the whole file and tripped over
        the word `pull_request` inside a comment explaining why there is no such
        trigger. A checker that cannot tell a rule from a note about the rule is
        the same species as a parser that reads a comment as code.
        """
        return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))

    def test_nothing_is_triggered_by_a_stranger(self):
        """No `pull_request` trigger: a stranger must not spend our minutes."""
        for name in ("tests.yml", "behaviour.yml"):
            text = self._uncommented(self.workflow(name))
            self.assertNotRegex(text, r"^\s+pull_request(_target)?:",
                                f"{name}: a fork could start this")
            self.assertIn("github.repository_owner ==", text,
                          f"{name}: a fork gets our workflow for free")

    def test_the_heavy_one_is_dispatch_only_and_queued(self):
        text = self.workflow("behaviour.yml")
        self.assertIn("workflow_dispatch", text)
        self.assertIn("concurrency:", text)
        self.assertIn("cancel-in-progress: false", text,
                      "runs have to queue rather than pile up or cancel each other")
        self.assertIn(f"max-parallel: {common.LIMITS['parallel_targets']}", text)

    def test_the_ceiling_comes_from_the_kit_rather_than_the_yaml(self):
        """The number of projects per run is read from `common.LIMITS` at run time."""
        text = self.workflow("behaviour.yml")
        self.assertIn("common.LIMITS['targets_per_run']", text)
        self.assertIn("common.LIMITS['clone_depth']", text)


class TestSweepKnowsEveryTool(unittest.TestCase):
    def test_sweep_plans_every_detector(self):
        """A tool the sweep does not know about will never be run."""
        s = source("sweep")
        for t in DETECTORS:
            self.assertIn(f'"{t}"', s, f"sweep does not know about {t}")

    def test_sweep_knows_the_pipeline(self):
        """Pipeline stages belong in the sweep too, otherwise they get run by hand
        and then forgotten."""
        s = source("sweep")
        for t in PIPELINE:
            if t in ("probe", "lessons"):
                # The prober runs against one finding and needs a test command;
                # the review harvester runs across all our pull requests at once.
                # Neither is tied to a project, so the sweep only names them in
                # the report instead of running them.
                self.assertIn(t, s, f"sweep does not name the {t} stage")
                continue
            self.assertIn(t, s, f"sweep does not know about the {t} stage")

    def test_sweep_counts_hard_by_the_agreed_key(self):
        self.assertIn('x.get("hard"', source("sweep"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
