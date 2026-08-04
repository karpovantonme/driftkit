#!/usr/bin/env python3
"""Tests for supportdrift.py: declared support against the CI matrix.

Classes of lie removed during development:

  3 findings on Go (prometheus, thanos, argo-cd). The `go` directive in go.mod
    sets the minimum language version; building with a newer toolchain is normal
    there, and the directive promises nothing about CI. No defect species at all.

  2 findings on nilearn. The real matrix hides behind `${{ env.MIN_PYTHON }}`.
    Once the matrix holds a value we cannot expand, our list of versions is
    knowingly incomplete, and "this version is not tested" cannot be claimed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import supportdrift as sd  # noqa: E402


def project(files: dict) -> str:
    root = tempfile.mkdtemp()
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


def run(files: dict) -> sd.Report:
    rep = sd.Report()
    sd.analyse(project(files), rep)
    return rep


PYPROJECT = """[project]
name = "x"
requires-python = ">=3.10,<4"
classifiers = [
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]
"""


def workflow(versions: str) -> str:
    return (
        "name: ci\non: [push]\njobs:\n  test:\n    strategy:\n      matrix:\n"
        f"        python-version: {versions}\n"
        "    steps:\n      - uses: actions/setup-python@v5\n"
        "        with:\n          python-version: ${{ matrix.python-version }}\n"
    )


class TestFindsRealGaps(unittest.TestCase):
    def test_classifier_claims_versions_ci_never_runs(self):
        """The live ecologits case: classifiers promise five versions while every
        workflow pins one."""
        rep = run({"pyproject.toml": PYPROJECT, ".github/workflows/ci.yml": workflow('["3.10"]')})
        hard = [f for f in rep.findings if f.hard]
        self.assertEqual([f.kind for f in hard], ["declared-not-tested"])
        self.assertIn("3.11", hard[0].message)
        self.assertIn("3.12", hard[0].message)

    def test_coordinates_point_at_the_claim_and_the_matrix(self):
        rep = run({"pyproject.toml": PYPROJECT, ".github/workflows/ci.yml": workflow('["3.10"]')})
        f = [x for x in rep.findings if x.hard][0]
        self.assertRegex(f.claim_ref, r"pyproject\.toml:\d+$")
        self.assertIn("ci.yml", f.ci_ref)

    def test_minimum_not_in_matrix_is_soft(self):
        rep = run(
            {
                "pyproject.toml": '[project]\nrequires-python = ">=3.9"\n',
                ".github/workflows/ci.yml": workflow('["3.12", "3.13"]'),
            }
        )
        self.assertEqual([f.kind for f in rep.findings], ["minimum-not-tested"])
        self.assertFalse(rep.findings[0].hard)


class TestSilence(unittest.TestCase):
    def test_everything_declared_is_tested(self):
        rep = run(
            {
                "pyproject.toml": PYPROJECT,
                ".github/workflows/ci.yml": workflow('["3.10", "3.11", "3.12"]'),
            }
        )
        self.assertEqual(rep.findings, [])

    def test_go_directive_is_not_a_promise_to_test(self):
        """go.mod sets the language minimum and promises nothing about CI runs.
        Three false findings came from this: prometheus, thanos, argo-cd."""
        rep = run(
            {
                "go.mod": "module x\n\ngo 1.25.8\n",
                ".github/workflows/ci.yml": (
                    "jobs:\n  t:\n    steps:\n      - uses: actions/setup-go@v5\n"
                    "        with:\n          go-version: 1.26\n"
                ),
            }
        )
        self.assertEqual(rep.findings, [])

    def test_opaque_matrix_value_blocks_any_claim(self):
        """nilearn keeps its matrix behind ${{ env.MIN_PYTHON }}. The list of
        versions is knowingly incomplete, so "not tested" cannot be claimed."""
        rep = run(
            {
                "pyproject.toml": PYPROJECT,
                ".github/workflows/ci.yml": (
                    "jobs:\n  t:\n    steps:\n      - uses: actions/setup-python@v5\n"
                    "        with:\n          python-version: ${{ env.MIN_PYTHON }}\n"
                ),
                ".github/workflows/other.yml": workflow('["3.10"]'),
            }
        )
        self.assertEqual(rep.findings, [])
        self.assertTrue(rep.opaque)
        self.assertTrue(any("cannot be expanded" in x for x in rep.no_matrix))

    def test_reference_to_a_parsed_matrix_key_is_not_opaque(self):
        """`${{ matrix.python-version }}` is the same matrix we have just read.
        Treating it as opaque hides real findings: the live finding in felupe
        was nearly lost that way."""
        rep = run({"pyproject.toml": PYPROJECT, ".github/workflows/ci.yml": workflow('["3.10"]')})
        self.assertEqual(rep.opaque, [])
        self.assertTrue([f for f in rep.findings if f.hard])

    def test_no_matrix_at_all(self):
        rep = run({"pyproject.toml": PYPROJECT})
        self.assertEqual(rep.findings, [])
        self.assertTrue(rep.no_matrix)

    def test_ci_below_declared_minimum_is_not_a_defect(self):
        rep = run(
            {
                "pyproject.toml": '[project]\nrequires-python = ">=3.10"\n',
                ".github/workflows/ci.yml": workflow('["3.9", "3.10"]'),
            }
        )
        self.assertEqual(rep.findings, [])
        self.assertTrue(rep.below_min)


class TestParsing(unittest.TestCase):
    def test_version_spec_forms(self):
        self.assertEqual(sd._min_from_spec(">=3.9,<4"), ["3.9"])
        self.assertEqual(sd._min_from_spec(">= 3.10"), ["3.10"])
        self.assertEqual(sd._min_from_spec("^18.0.0"), ["18.0.0"])

    def test_column_style_matrix(self):
        rep = run(
            {
                "pyproject.toml": PYPROJECT,
                ".github/workflows/ci.yml": (
                    "jobs:\n  t:\n    strategy:\n      matrix:\n"
                    "        python-version:\n          - '3.10'\n          - '3.11'\n          - '3.12'\n"
                ),
            }
        )
        self.assertEqual(rep.findings, [])

    def test_patch_level_difference_is_not_a_gap(self):
        rep = run(
            {
                "Cargo.toml": '[package]\nrust-version = "1.86.0"\n',
                ".github/workflows/ci.yml": (
                    "jobs:\n  t:\n    steps:\n      - uses: dtolnay/rust-toolchain@v1\n"
                    "        with:\n          toolchain: 1.86.1\n"
                ),
            }
        )
        self.assertEqual(rep.findings, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
