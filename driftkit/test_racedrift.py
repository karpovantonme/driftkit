#!/usr/bin/env python3
"""Tests for racedrift.py: promises about concurrency against behaviour.

No suite is ever run here. What gets tested is the part that decides **where**
this check may run and how it reads what the detector printed, because those are
the two places where it can do harm: on the wrong machine, or by inventing a
race out of ordinary output.

Run: python3 test_racedrift.py
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import racedrift


RACE_OUTPUT = """==================
WARNING: DATA RACE
Write at 0x00c000180010 by goroutine 8:
  /work/target/internal/cache/store.go:57 +0x64
  /work/target/internal/cache/store.go:120 +0x2c

Previous read at 0x00c000180010 by goroutine 7:
  /work/target/internal/cache/store.go:88 +0x40
==================
--- FAIL: TestConcurrentWrites (0.02s)
FAIL	example.com/target/internal/cache	0.312s
"""

CLEAN_OUTPUT = """ok  	example.com/target/internal/cache	0.312s
ok  	example.com/target/internal/api	1.204s
"""


class TestWhereItMayRun(unittest.TestCase):
    """The half that keeps somebody else's test suite off a laptop."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        pathlib.Path(self.dir, "go.mod").write_text("module target\n\ngo 1.21\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_check_is_declared_as_a_build_check(self):
        """Running a foreign suite belongs on a disposable machine."""
        self.assertEqual(common.place_of("racedrift"), common.BUILD)
        self.assertFalse(common.runs_here("racedrift"))

    def test_it_refuses_here_by_default(self):
        rep = racedrift.Report()
        racedrift.analyse(self.dir, rep, timeout=5, allow_here=False)
        self.assertFalse(rep.ran)
        self.assertTrue(any("disposable runner" in n for n in rep.notes))

    def test_it_still_says_what_it_would_have_run(self):
        """A refusal that reports nothing about the project is a wasted run."""
        rep = racedrift.Report()
        racedrift.analyse(self.dir, rep, timeout=5, allow_here=False)
        self.assertEqual(rep.system, "go")

    def test_another_language_is_named_rather_than_guessed(self):
        d = tempfile.mkdtemp()
        try:
            pathlib.Path(d, "Cargo.toml").write_text("[package]\n", encoding="utf-8")
            rep = racedrift.Report()
            racedrift.analyse(d, rep, timeout=5, allow_here=True)
            self.assertFalse(rep.ran)
            self.assertTrue(any("only Go is implemented" in n for n in rep.notes))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestReadingTheOutput(unittest.TestCase):
    """The half that decides what the detector actually said."""

    def test_a_race_becomes_one_finding_with_a_coordinate(self):
        hits = racedrift.read_output(RACE_OUTPUT, "/work/target", ["races"])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].file, "internal/cache/store.go")
        self.assertEqual(hits[0].line, 57)
        self.assertTrue(hits[0].hard)

    def test_the_same_race_twice_is_one_finding(self):
        """A suite reruns packages; one trouble is one finding, as everywhere."""
        hits = racedrift.read_output(RACE_OUTPUT + RACE_OUTPUT, "/work/target", ["races"])
        self.assertEqual(len(hits), 1)

    def test_a_promised_detector_changes_what_the_finding_means(self):
        """CI advertising `-race` and failing under it is the stronger case."""
        promised = racedrift.read_output(RACE_OUTPUT, "/work/target", ["races"])
        silent = racedrift.read_output(RACE_OUTPUT, "/work/target", [])
        self.assertEqual(promised[0].kind, "race-under-promised-detector")
        self.assertEqual(silent[0].kind, "race-detector-not-in-ci")

    def test_a_clean_run_is_no_finding(self):
        self.assertEqual(racedrift.read_output(CLEAN_OUTPUT, "/work/target", ["races"]), [])

    def test_a_failing_suite_without_a_race_is_not_ours(self):
        """A broken or flaky suite is the project's business, not a finding."""
        out = "--- FAIL: TestSomething (0.01s)\nFAIL\texample.com/target\t0.2s\n"
        self.assertEqual(racedrift.read_output(out, "/work/target", ["races"]), [])


class TestContract(unittest.TestCase):
    def test_json_and_exit_code(self):
        d = tempfile.mkdtemp()
        try:
            pathlib.Path(d, "go.mod").write_text("module t\n", encoding="utf-8")
            out = os.path.join(d, "out.json")
            code = racedrift.main(["--dir", d, "--json", out])
            self.assertEqual(code, 0)
            self.assertEqual(json.load(open(out, encoding="utf-8")), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_ceiling_comes_from_the_shared_limits(self):
        """One place declares how long a job may live, and it is not this file."""
        import re
        src = pathlib.Path(__file__).with_name("racedrift.py").read_text(encoding="utf-8")
        self.assertIn('common.LIMITS["job_timeout_minutes"]', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
