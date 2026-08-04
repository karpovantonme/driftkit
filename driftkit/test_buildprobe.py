#!/usr/bin/env python3
"""Tests for buildprobe.py. Run: python3 test_buildprobe.py"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import buildprobe
from buildprobe import Probe, System


def tree(root, files):
    """files: {'relative/path': 'content'}"""
    for rel, body in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


class TreeCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------- recognising the system


class TestDetect(TreeCase):
    def test_go_at_the_root(self):
        tree(self.dir, {"go.mod": "module x\n", "main.go": "package main\n"})
        sysd, sub = buildprobe.detect(self.dir)
        self.assertEqual(sysd.name, "go")
        self.assertEqual(sub, "")

    def test_rust_at_the_root(self):
        tree(self.dir, {"Cargo.toml": "[package]\nname='x'\n"})
        self.assertEqual(buildprobe.detect(self.dir)[0].name, "rust")

    def test_makefile_comes_last(self):
        """A Makefile sits next to anything and names no language by itself.

        qdrant, rclone and etcd all keep one at the root. Taking it first would
        turn every project into "make" and the detection would lose its point.
        """
        tree(self.dir, {"Makefile": "all:\n\techo\n", "go.mod": "module x\n"})
        self.assertEqual(buildprobe.detect(self.dir)[0].name, "go")

    def test_makefile_only(self):
        tree(self.dir, {"Makefile": "all:\n\techo\n"})
        self.assertEqual(buildprobe.detect(self.dir)[0].name, "make")

    def test_one_level_down(self):
        tree(self.dir, {"src/go.mod": "module x\n"})
        sysd, sub = buildprobe.detect(self.dir)
        self.assertEqual(sysd.name, "go")
        self.assertEqual(sub, "src")

    def test_deeper_than_one_level_is_not_searched(self):
        """A deliberate limit, stated in the module header."""
        tree(self.dir, {"a/b/go.mod": "module x\n"})
        self.assertIsNone(buildprobe.detect(self.dir)[0])

    def test_vendor_does_not_count(self):
        """In Go projects `vendor/` is full of other people's go.mod files."""
        tree(self.dir, {"vendor/foo/go.mod": "module foo\n"})
        self.assertIsNone(buildprobe.detect(self.dir)[0])

    def test_node_modules_does_not_count(self):
        tree(self.dir, {"node_modules/x/package.json": "{}"})
        self.assertIsNone(buildprobe.detect(self.dir)[0])

    def test_hidden_directories_do_not_count(self):
        tree(self.dir, {".cache/go.mod": "module x\n"})
        self.assertIsNone(buildprobe.detect(self.dir)[0])

    def test_empty_tree(self):
        self.assertEqual(buildprobe.detect(self.dir), (None, ""))

    def test_missing_directory(self):
        pr = buildprobe.probe_one(os.path.join(self.dir, "no-such-dir"))
        self.assertIsNone(pr.system)
        self.assertIn("no such directory", pr.notes)


# ------------------------------------------------------- dynamic markers


class TestDynamic(TreeCase):
    def test_race_flag_in_a_workflow(self):
        tree(self.dir, {".github/workflows/ci.yml": "run: go test -race ./...\n"})
        self.assertIn("races", buildprobe.dynamic_marks(self.dir))

    def test_sanitizers(self):
        tree(self.dir, {".github/workflows/ci.yml": "run: cmake -DCMAKE_C_FLAGS=-fsanitize=address .\n"})
        self.assertIn("sanitizers", buildprobe.dynamic_marks(self.dir))

    def test_fuzzing_in_a_makefile(self):
        tree(self.dir, {"Makefile": "fuzz:\n\tgo test -fuzz=Fuzz\n"})
        self.assertIn("fuzzing", buildprobe.dynamic_marks(self.dir))

    def test_github_is_read_despite_the_dot(self):
        """A blanket hidden-directory mask once dropped `.github`.

        The mistake was silent: the report simply looked tidier. That costs more
        than any false finding, because a gap like this is invisible.
        """
        tree(self.dir, {".github/workflows/a.yaml": "run: pytest --cov\n"})
        self.assertIn("coverage", buildprobe.dynamic_marks(self.dir))

    def test_non_yaml_in_workflows_is_skipped(self):
        tree(self.dir, {".github/workflows/README.md": "we use -fsanitize=address\n"})
        self.assertEqual(buildprobe.dynamic_marks(self.dir), [])

    def test_nothing_found(self):
        tree(self.dir, {".github/workflows/ci.yml": "run: echo hello\n"})
        self.assertEqual(buildprobe.dynamic_marks(self.dir), [])

    def test_missing_directory_does_not_crash(self):
        self.assertEqual(buildprobe.dynamic_marks(self.dir), [])


# ------------------------------------------------------------ running and refusing


class TestRun(TreeCase):
    def test_without_run_nothing_is_executed(self):
        """The sweep walks a hundred foreign clones: looking is not executing."""
        tree(self.dir, {"Makefile": "all:\n\ttouch /tmp/MUST-NOT-APPEAR\n"})
        pr = buildprobe.probe_one(self.dir, run=False)
        self.assertFalse(pr.ran)
        self.assertIsNone(pr.built)
        self.assertTrue(any("no run requested" in n for n in pr.notes))
        self.assertFalse(os.path.exists("/tmp/MUST-NOT-APPEAR"))

    def test_successful_build(self):
        ok, sec, tail = buildprobe.run_build("exit 0", self.dir, timeout=30)
        self.assertTrue(ok)

    def test_failed_build_with_a_tail(self):
        ok, sec, tail = buildprobe.run_build("echo trouble >&2; exit 2", self.dir, timeout=30)
        self.assertFalse(ok)
        self.assertIn("trouble", tail)

    def test_timeout(self):
        ok, sec, tail = buildprobe.run_build("sleep 5", self.dir, timeout=1)
        self.assertFalse(ok)
        self.assertIn("did not finish", tail)

    def test_tool_not_installed(self):
        tree(self.dir, {"Cargo.toml": "[package]\n"})
        pr = buildprobe.probe_one(self.dir, run=True)
        if shutil.which("cargo") is None:
            self.assertFalse(pr.tool_present)
            self.assertFalse(pr.ran)
            self.assertIn("`cargo` is not installed", pr.verdict)
            self.assertFalse(pr.usable)   # not usable while cargo is missing
        else:
            self.skipTest("cargo is installed, the case cannot be reproduced")


# --------------------------------------------------------------- verdicts


class TestVerdict(unittest.TestCase):
    def sys(self):
        return System("go", "go.mod", "go", "go build ./...", "go test ./...")

    def test_not_recognised(self):
        self.assertEqual(Probe(root="/x").verdict, "build system not recognised")

    def test_not_attempted_names_the_command(self):
        p = Probe(root="/x", system=self.sys(), tool_present=True)
        self.assertIn("go build ./...", p.verdict)

    def test_it_builds(self):
        p = Probe(root="/x", system=self.sys(), tool_present=True, ran=True,
                  built=True, seconds=73.4)
        self.assertIn("builds in 73 s", p.verdict)

    def test_failed_build_is_not_usable(self):
        p = Probe(root="/x", system=self.sys(), tool_present=True, ran=True, built=False)
        self.assertIn("does NOT build", p.verdict)
        self.assertFalse(p.usable)

    def test_not_attempted_is_still_usable(self):
        """A project that was never built is not declared unusable."""
        p = Probe(root="/x", system=self.sys(), tool_present=True)
        self.assertTrue(p.usable)


# ------------------------------------------------------------- kit contract


class TestContract(TreeCase):
    def test_json_and_exit_code(self):
        tree(self.dir, {"go.mod": "module x\n"})
        out = os.path.join(self.dir, "out.json")
        code = buildprobe.main([self.dir, "--json", out])
        self.assertEqual(code, 0)   # usability is no finding
        data = json.load(open(out, encoding="utf-8"))
        self.assertTrue(data)
        for x in data:
            self.assertIn("hard", x)
            self.assertFalse(x["hard"])   # hard findings never happen here

    def test_the_report_says_zero_hard(self):
        """A usable project is a soft line in the report."""
        import io
        from contextlib import redirect_stdout
        tree(self.dir, {"go.mod": "module x\n"})
        buf = io.StringIO()
        with redirect_stdout(buf):
            buildprobe.print_report([buildprobe.probe_one(self.dir)])
        text = buf.getvalue()
        self.assertIn("=== Coverage ===", text)
        self.assertIn("0 hard", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
