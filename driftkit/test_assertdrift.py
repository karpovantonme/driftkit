#!/usr/bin/env python3
"""Tests for assertdrift.py: a test that does not check what it claims.

Positive control is the traefik case before pull request #1955. `errorMessage`
sat at the same indentation as `failedTemplate`, YAML read that as two sibling
keys, and the error text was never compared. The clone has it fixed by now, so
a live run stays quiet; the case is reconstructed synthetically here.

The class of lie removed during development: `template`, `documentIndex` and
`not` sit beside an assertion BY DESIGN. Without a list of legitimate siblings
the tool produced 85 "findings" on a single traefik chart.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import assertdrift as ad  # noqa: E402

TRAEFIK = os.path.expanduser("~/Projects/oss/traefik/traefik-helm-chart")
HAS_TRAEFIK = os.path.isdir(TRAEFIK)


def run(body: str) -> ad.Report:
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "tests"))
    with open(os.path.join(root, "tests", "a_test.yaml"), "w", encoding="utf-8") as fh:
        fh.write(body)
    rep = ad.Report()
    ad.analyse(root, rep)
    return rep


BROKEN = """suite: requirements
templates:
  - _helpers.tpl
tests:
  - it: should fail on old version
    set:
      image.tag: v3.5
    asserts:
      - failedTemplate:
        errorMessage: "ERROR: only v3.6.0+ is supported"
"""

FIXED = """suite: requirements
templates:
  - _helpers.tpl
tests:
  - it: should fail on old version
    set:
      image.tag: v3.5
    asserts:
      - failedTemplate:
          errorMessage: "ERROR: only v3.6.0+ is supported"
"""


class TestKnownCase(unittest.TestCase):
    """traefik before pull request #1955."""

    def test_stray_child_key_is_found(self):
        rep = run(BROKEN)
        self.assertEqual(len(rep.findings), 1)
        f = rep.findings[0]
        self.assertEqual(f.assertion, "failedTemplate")
        self.assertEqual(f.stray, ["errorMessage"])

    def test_correct_nesting_is_silent(self):
        rep = run(FIXED)
        self.assertEqual(rep.findings, [])
        self.assertEqual(rep.assertions, 1)

    def test_finding_carries_coordinate_test_name_and_probe(self):
        f = run(BROKEN).findings[0]
        self.assertTrue(f.file.endswith("a_test.yaml"))
        self.assertGreater(f.line, 0)
        self.assertIn("old version", f.test_name)
        self.assertIn("must fail", f.probe)


class TestSilence(unittest.TestCase):
    def test_legit_siblings_are_not_findings(self):
        """`template`, `documentIndex` and `not` are assertion modifiers.
        Without that list a single traefik chart produced 85 false findings."""
        body = """suite: s
tests:
  - it: t
    asserts:
      - equal:
          path: spec.replicas
          value: 3
        template: deployment.yaml
        documentIndex: 1
      - contains:
          path: spec.rules
          content: {host: a}
        not: true
"""
        rep = run(body)
        self.assertEqual(rep.findings, [])
        self.assertGreaterEqual(rep.legit_siblings, 3)

    def test_assertion_with_content_is_not_judged(self):
        """When an assertion already has nested content, the sibling key may be
        something else entirely. Nothing to claim here."""
        body = """suite: s
tests:
  - it: t
    asserts:
      - equal:
          path: a
          value: 1
        value: 2
"""
        self.assertEqual(run(body).findings, [])

    def test_two_assertions_in_one_block_are_not_judged(self):
        """Whose child key this is cannot be established, so no guessing."""
        body = """suite: s
tests:
  - it: t
    asserts:
      - equal:
        matchRegex:
        path: a
"""
        self.assertEqual(run(body).findings, [])

    def test_broken_yaml_is_reported_not_swallowed(self):
        rep = run("suite: s\ntests:\n  - it: t\n   asserts: [\n")
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.unparsed), 1)

    def test_non_test_yaml_is_ignored(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "values.yaml"), "w", encoding="utf-8") as fh:
            fh.write("failedTemplate:\nerrorMessage: x\n")
        rep = ad.Report()
        ad.analyse(root, rep)
        self.assertEqual(rep.files, 0)


class TestOtherAssertions(unittest.TestCase):
    def test_equal_missing_its_path_and_value(self):
        body = """suite: s
tests:
  - it: t
    asserts:
      - equal:
        path: spec.replicas
        value: 3
"""
        rep = run(body)
        self.assertEqual([f.assertion for f in rep.findings], ["equal"])
        self.assertEqual(rep.findings[0].stray, ["path", "value"])

    def test_matchregex_missing_its_pattern(self):
        body = """suite: s
tests:
  - it: t
    asserts:
      - matchRegex:
        path: metadata.name
        pattern: ^traefik
"""
        rep = run(body)
        self.assertEqual([f.assertion for f in rep.findings], ["matchRegex"])


@unittest.skipUnless(HAS_TRAEFIK, "no traefik-helm-chart clone")
class TestOnRealChart(unittest.TestCase):
    """Negative control: after pull request #1955 there is nothing there."""

    @classmethod
    def setUpClass(cls):
        cls.rep = ad.Report()
        ad.analyse(TRAEFIK, cls.rep)

    def test_no_findings_after_the_fix(self):
        self.assertEqual([f"{f.file}:{f.line}" for f in self.rep.findings], [])

    def test_work_was_actually_done(self):
        self.assertGreater(self.rep.files, 20)
        self.assertGreater(self.rep.assertions, 500)
        self.assertEqual(self.rep.unparsed, [], "file not parsed, so the report lies")


if __name__ == "__main__":
    unittest.main(verbosity=2)
