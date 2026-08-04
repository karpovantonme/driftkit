#!/usr/bin/env python3
"""Tests for probe.py.

The prober edits a file inside someone else's working tree, so the main test is
about **safety** rather than verdicts: the file has to come back byte for byte
on any outcome, exceptions inside the run included.

The live case the tool is built on is traefik #1955. With the indentation fixed,
replacing the expected text breaks the test; without the fix it does not, since
YAML reads `errorMessage` as a sibling key and the message is never compared.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import probe  # noqa: E402

YAML = """suite: requirements
tests:
  - it: should fail on old version
    asserts:
      - failedTemplate:
          errorMessage: "ERROR: only v3.6.0+ is supported"
"""


def project(files: dict, git: bool = True) -> str:
    root = tempfile.mkdtemp()
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    if git:
        subprocess.run(["git", "init", "-q", root], capture_output=True)
        subprocess.run(["git", "-C", root, "add", "-A"], capture_output=True)
        subprocess.run(
            ["git", "-C", root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            capture_output=True,
        )
    return root


# Fake "project tests": the command fails when the file lacks the expected line.
CHECKER = (
    "python3 -c \"import sys;"
    "sys.exit(0 if 'only v3.6.0+ is supported' in open('t/a_test.yaml').read() else 1)\""
)
BLIND = "python3 -c \"import sys; sys.exit(0)\""


class TestKeyLookup(unittest.TestCase):
    def test_finds_key_and_value(self):
        got = probe.find_key(YAML, "errorMessage")
        self.assertIsNotNone(got)
        line, prefix, value = got
        self.assertEqual(line, 6)
        self.assertIn("errorMessage", prefix)
        self.assertIn("only v3.6.0+", value)

    def test_missing_key(self):
        self.assertIsNone(probe.find_key(YAML, "noSuchKey"))

    def test_mutation_keeps_the_prefix_and_replaces_the_value(self):
        line, prefix, value = probe.find_key(YAML, "errorMessage")
        out = probe.mutate(YAML, line, prefix, value)
        self.assertIn(probe.MUTATION, out)
        self.assertNotIn("only v3.6.0+", out)
        self.assertEqual(len(out.splitlines()), len(YAML.splitlines()))


class TestVerdicts(unittest.TestCase):
    def test_test_that_compares_is_a_false_finding(self):
        """The test fails after the mutation, so it does compare the value."""
        root = project({"t/a_test.yaml": YAML})
        res = probe.probe("t/a_test.yaml", "errorMessage", CHECKER, root)
        self.assertFalse(res.baseline_failed)
        self.assertTrue(res.mutated_failed)
        self.assertFalse(res.proven)
        self.assertIn("does compare it", res.verdict)

    def test_test_that_does_not_compare_is_proven(self):
        """The test survives the mutation, so it does not compare the value.
        That is the proof this species needs."""
        root = project({"t/a_test.yaml": YAML})
        res = probe.probe("t/a_test.yaml", "errorMessage", BLIND, root)
        self.assertFalse(res.baseline_failed)
        self.assertFalse(res.mutated_failed)
        self.assertTrue(res.proven)
        self.assertIn("does NOT compare", res.verdict)

    def test_red_baseline_stops_before_mutating(self):
        """When the tests fail without the mutation, probing is meaningless."""
        root = project({"t/a_test.yaml": YAML})
        res = probe.probe("t/a_test.yaml", "errorMessage", "python3 -c \"import sys;sys.exit(1)\"", root)
        self.assertTrue(res.baseline_failed)
        self.assertIsNone(res.mutated_failed)
        self.assertFalse(res.proven)


class TestSafety(unittest.TestCase):
    def test_file_is_restored_byte_for_byte(self):
        root = project({"t/a_test.yaml": YAML})
        probe.probe("t/a_test.yaml", "errorMessage", BLIND, root)
        with open(os.path.join(root, "t/a_test.yaml"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), YAML)

    def test_restore_happens_even_when_the_command_is_broken(self):
        root = project({"t/a_test.yaml": YAML})
        res = probe.probe("t/a_test.yaml", "errorMessage", "no-such-command-probe", root)
        with open(os.path.join(root, "t/a_test.yaml"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), YAML)
        self.assertTrue(res.baseline_failed)

    def test_dirty_tree_is_not_touched(self):
        """The mutation would mix with someone else's edits."""
        root = project({"t/a_test.yaml": YAML})
        with open(os.path.join(root, "t/a_test.yaml"), "a", encoding="utf-8") as fh:
            fh.write("# someone else's edit\n")
        res = probe.probe("t/a_test.yaml", "errorMessage", BLIND, root)
        self.assertIsNone(res.baseline_failed)
        self.assertTrue(any("dirty" in n for n in res.notes))
        with open(os.path.join(root, "t/a_test.yaml"), encoding="utf-8") as fh:
            self.assertIn("someone else's edit", fh.read())

    def test_dry_run_changes_nothing(self):
        root = project({"t/a_test.yaml": YAML})
        res = probe.probe("t/a_test.yaml", "errorMessage", BLIND, root, dry=True)
        self.assertIsNone(res.baseline_failed)
        with open(os.path.join(root, "t/a_test.yaml"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), YAML)

    def test_missing_key_does_not_run_anything(self):
        root = project({"t/a_test.yaml": YAML})
        res = probe.probe("t/a_test.yaml", "noSuchKey", BLIND, root)
        self.assertIsNone(res.baseline_failed)
        self.assertTrue(any("not found" in n for n in res.notes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
