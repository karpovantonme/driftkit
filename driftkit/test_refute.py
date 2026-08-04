#!/usr/bin/env python3
"""Tests for refute.py.

The second stage of the pipeline, between "the tool said so" and "a human took
it somewhere". Every refutation comes from a live case and each one is pinned
down here.

The property tested separately from the rest: **refuting one coordinate does not
kill a finding**. Comparators carry two, and while one side is alive the finding
is alive. Otherwise the refuter would start hiding real findings, which is worse
than letting false ones through.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import refute  # noqa: E402


def project(files: dict) -> str:
    root = tempfile.mkdtemp()
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


def run(findings, files: dict) -> refute.Report:
    rep = refute.Report()
    refute.analyse(findings, project(files), rep)
    return rep


class TestCoordExtraction(unittest.TestCase):
    """Field names differ across the tools, so the search goes by the shape of
    the value rather than the key name. A new tool gets picked up by itself."""

    def test_every_tool_shape(self):
        cases = [
            ({"proto": "a.proto:12", "openapi": "b.json:44"}, 2),          # ifacedrift
            ({"copy": "x.go:38", "commit": "https://github.com/a/b/commit/c"}, 1),  # liftdrift
            ({"original": "content/en/a.md:5", "translation": "content/ja/a.md"}, 1),  # transdrift
            ({"field_ref": "s.go:7", "func_ref": "s.go:20", "struct_ref": "s.go:3"}, 3),  # gitdrift
            ({"claim": "pyproject.toml:6", "ci": ".github/workflows/ci.yml"}, 1),  # supportdrift
            ({"a_ref": "d.md:3", "b_ref": "e.h:9"}, 2),                    # namedrift
            ({"where": "docs/a.md:11", "changelog": "CHANGELOG.md:99"}, 2),  # deaddrift
            ({"file": "t.yaml", "line": 4}, 1),                            # assertdrift, docdrift: two fields
            ({"file": "/abs/mne/matrix.py", "line": 97, "param": "matkind"}, 1),  # docdrift
            ({"file": "include/a.hpp", "line": 3, "name": "T"}, 1),        # doxdrift
            ({"refs": ["a.md:1", "b.md:2", "a.md:1"]}, 2),                 # linkdrift, with a duplicate
        ]
        for finding, want in cases:
            self.assertEqual(len(refute.coords_of(finding)), want, finding)

    def test_a_split_coordinate_is_read(self):
        """`file` plus `line` separately used to read as "no coordinates".

        On nineteen docdrift findings in mne the refuter honestly printed
        "nothing to refute against" and checked none of them. The mistake was
        silent: it only made the report look tidier, the same family as the
        dropped `.github` directory.
        """
        self.assertEqual(refute.coords_of({"file": "a/b.py", "line": 12}), [("a/b.py", 12)])

    def test_line_zero_is_no_coordinate(self):
        self.assertEqual(refute.coords_of({"file": "a/b.py", "line": 0}), [])

    def test_a_boolean_is_no_line_number(self):
        self.assertEqual(refute.coords_of({"file": "a/b.py", "line": True}), [])

    def test_without_a_path_there_is_no_coordinate(self):
        self.assertEqual(refute.coords_of({"count": 5, "line": 12}), [])

    def test_a_finding_about_a_comment_survives_the_comment_rule(self):
        """doxdrift reports the line of a `\\param` block, a comment by construction.

        The rule "the coordinate points at a comment rather than code" wiped out
        24 real findings out of 24 on Boost.Geometry. The second case in the whole
        history where the refuter worked against us.
        """
        finding = {"tool": "doxdrift", "kind": "tparam", "name": "Geometry1",
                   "file": "a.hpp", "line": 2, "decl": "template <class G> void f();"}
        rep = run([finding], {"a.hpp": "code\n/// \\tparam Geometry1 first\nvoid f();\n"})
        self.assertEqual(len(rep.survivors), 1, "a finding about a comment was killed")

    def test_a_finding_about_code_is_killed_by_the_comment_rule(self):
        """For detectors that judge code the rule still holds."""
        finding = {"tool": "gitdrift", "file": "a.go", "line": 2}
        rep = run([finding], {"a.go": "code\n// just a comment\ncode\n"})
        self.assertEqual(len(rep.survivors), 0)

    def test_url_with_port_is_not_a_coordinate(self):
        self.assertEqual(refute.coords_of({"url": "https://example.com:8080"}), [])

    def test_finding_without_coordinates_is_not_silently_dropped(self):
        rep = run([{"message": "no coordinates"}], {"a.md": "x\n"})
        self.assertEqual(len(rep.no_coords), 1)
        self.assertEqual(len(rep.verdicts), 1)


class TestRefutations(unittest.TestCase):
    def test_generated_file(self):
        """The otelcol case: 12 candidates sat in generated_metrics.go."""
        rep = run(
            [{"where": "gen.go:3"}],
            {"gen.go": "// Code generated by mdatagen. DO NOT EDIT.\n\npackage p\nvar X = 1\n"},
        )
        self.assertFalse(rep.verdicts[0].survives)
        self.assertTrue(any("generated" in k for k in rep.verdicts[0].killed_by))

    def test_archived_path(self):
        rep = run([{"where": "docs/v3.5/config.md:2"}], {"docs/v3.5/config.md": "a\nb\n"})
        self.assertFalse(rep.verdicts[0].survives)

    def test_fixtures_path(self):
        rep = run([{"where": "testdata/sample.md:1"}], {"testdata/sample.md": "a\n"})
        self.assertFalse(rep.verdicts[0].survives)

    def test_qualified_nearby(self):
        """The Prometheus case: every mention carries an honest version caveat."""
        rep = run(
            [{"where": "docs/a.md:3"}],
            {"docs/a.md": "# Legacy\n\nFor versions v2.38 and below you could set this.\nmore\n"},
        )
        self.assertFalse(rep.verdicts[0].survives)

    def test_bare_version_numbers_are_not_a_qualification(self):
        """The classifier list in pyproject.toml consists of version numbers
        entirely. Under the old rule the refuter killed a real finding in
        ecologits. Killing a real one is worse than letting a false one through."""
        rep = run(
            [{"claim": "pyproject.toml:4"}],
            {
                "pyproject.toml": (
                    "[project]\nclassifiers = [\n"
                    '  "Programming Language :: Python :: 3.10",\n'
                    '  "Programming Language :: Python :: 3.11",\n'
                    '  "Programming Language :: Python :: 3.12",\n]\n'
                )
            },
        )
        self.assertTrue(rep.verdicts[0].survives, rep.verdicts[0].killed_by)

    def test_coordinate_past_end_of_file(self):
        rep = run([{"where": "a.go:999"}], {"a.go": "package p\n"})
        self.assertFalse(rep.verdicts[0].survives)
        self.assertTrue(any("past the end" in k for k in rep.verdicts[0].killed_by))

    def test_line_is_a_comment_in_code(self):
        rep = run([{"where": "a.go:2"}], {"a.go": "package p\n// var Old = 1\nvar New = 2\n"})
        self.assertFalse(rep.verdicts[0].survives)

    def test_clean_finding_survives(self):
        rep = run([{"where": "a.go:3"}], {"a.go": "package p\n\nvar Real = 1\n"})
        self.assertTrue(rep.verdicts[0].survives)
        self.assertEqual(rep.verdicts[0].killed_by, [])

    def test_missing_file_is_unknown_not_a_kill(self):
        """No such file: the coordinate may be relative to another root.
        That is "unknown" rather than "refuted"."""
        rep = run([{"where": "gone.go:3"}], {"a.go": "package p\n"})
        self.assertTrue(rep.verdicts[0].survives)
        self.assertTrue(rep.verdicts[0].unknown)


class TestTwoSidedFindings(unittest.TestCase):
    def test_one_dead_side_does_not_kill_the_finding(self):
        """Comparators carry two coordinates. While one is alive the finding is
        alive. Otherwise the refuter starts hiding real findings."""
        rep = run(
            [{"proto": "api.proto:5", "openapi": "docs/v1/spec.json:2"}],
            {"api.proto": "syntax = \"proto3\";\npackage p;\nmessage M {\n int32 a = 1;\n}\n",
             "docs/v1/spec.json": "{\n}\n"},
        )
        self.assertTrue(rep.verdicts[0].survives, rep.verdicts[0].killed_by)

    def test_both_sides_dead_kills_it(self):
        rep = run(
            [{"proto": "testdata/api.proto:1", "openapi": "docs/v1/spec.json:1"}],
            {"testdata/api.proto": "x\n", "docs/v1/spec.json": "y\n"},
        )
        self.assertFalse(rep.verdicts[0].survives)


class TestClusters(unittest.TestCase):
    def test_many_findings_in_one_file_are_named_as_one_trouble(self):
        """A kit-wide rule derived three times: 115 mismatches in qdrant, 17
        links on one page of a Chinese translation, 81 flags in sarek."""
        findings = [{"where": f"docs/a.md:{i}"} for i in range(1, 9)]
        rep = run(findings, {"docs/a.md": "\n".join(f"line {i}" for i in range(1, 20))})
        self.assertTrue(rep.clusters)
        self.assertIn("8 findings", rep.clusters[0])

    def test_few_findings_are_not_a_cluster(self):
        findings = [{"where": f"docs/a.md:{i}"} for i in (1, 2)]
        rep = run(findings, {"docs/a.md": "a\nb\nc\n"})
        self.assertEqual(rep.clusters, [])


class TestOnRealToolOutput(unittest.TestCase):
    """The refuter has to accept the output of any tool in the kit without
    being adjusted for each one."""

    def test_accepts_shapes_of_all_nine_tools(self):
        import glob

        here = os.path.dirname(os.path.abspath(__file__))
        tools = [
            os.path.basename(p)[:-3]
            for p in glob.glob(os.path.join(here, "*drift.py"))
        ]
        self.assertGreaterEqual(len(tools), 8, tools)


if __name__ == "__main__":
    unittest.main(verbosity=2)
