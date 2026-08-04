#!/usr/bin/env python3
"""Tests for namedrift.py: one name spelled two ways.

Positive control is the historical AFL++ case before our fix #2865. The docs
knew `AFL_GCC_ONLY_FSRV` while the `envs.h` registry had `AFL_GCC_ONLY_FRSV`.
Today both spellings sit in the clone on purpose, so a live run gives a soft
line instead of a finding. The case is reconstructed synthetically here.

Classes of lie removed during development on live AFL++:

  162 "findings". Allowing insertion and deletion catches families of related
      names: `--disable-docs` against `--disable-bochs`, `XXH_HAS_ATTRIBUTE`
      against `XXH_HAS_C_ATTRIBUTE`.

   18 of 19. Substitution inside a short segment: `GUM_X86_EBX` against
      `GUM_X86_RBX` are the 32-bit and 64-bit registers, `ARM64_REG_W29`
      against `X29` likewise.

    1. `AFL_DEFER_FORKSVR` turned out to be a C variable name, invisible to
      users. The mismatch is real and cosmetic.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import namedrift as nd  # noqa: E402

AFLPP = os.path.expanduser("~/Projects/oss/aflpp")
HAS_AFLPP = os.path.isdir(AFLPP)


def project(files: dict) -> str:
    root = tempfile.mkdtemp()
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


def run(files: dict) -> nd.Report:
    rep = nd.Report()
    nd.analyse(project(files), rep)
    return rep


def hard(rep) -> list:
    return [f for f in rep.findings if f.hard]


class TestKnownCase(unittest.TestCase):
    """AFL++ before fix #2865."""

    FILES = {
        "docs/env_variables.md": (
            "- `AFL_LLVM_ONLY_FSRV`/`AFL_GCC_ONLY_FSRV` will inject forkserver\n"
            "  but not pc instrumentation.\n"
        ),
        "include/envs.h": (
            'static const char *afl_environment_variables[] = {\n'
            '    "AFL_LLVM_ONLY_FSRV", "AFL_GCC_ONLY_FRSV",\n'
            "};\n"
        ),
        "src/afl-cc.c": '  if (getenv("AFL_GCC_ONLY_FRSV")) { do_something(); }\n',
    }

    def test_transposition_across_files_is_a_hard_finding(self):
        rep = run(self.FILES)
        h = hard(rep)
        self.assertEqual(len(h), 1, [f"{f.a}/{f.b}" for f in h])
        self.assertEqual({h[0].a, h[0].b}, {"AFL_GCC_ONLY_FSRV", "AFL_GCC_ONLY_FRSV"})
        self.assertEqual(h[0].shape, "transposition")

    def test_finding_carries_coordinates_and_counts(self):
        f = hard(run(self.FILES))[0]
        for ref in (f.a_ref, f.b_ref):
            self.assertRegex(ref, r"^.+:\d+$")
        self.assertGreaterEqual(f.a_count + f.b_count, 3)

    def test_same_file_pair_is_soft(self):
        """After fix #2865 both spellings live in envs.h on purpose."""
        files = dict(self.FILES)
        files["include/envs.h"] = (
            'static const char *v[] = {\n'
            '    "AFL_GCC_ONLY_FSRV", "AFL_GCC_ONLY_FRSV",\n'
            "};\n"
        )
        rep = run(files)
        self.assertEqual(hard(rep), [])
        self.assertEqual(len(rep.findings), 1)


class TestSilenceOnFamilies(unittest.TestCase):
    def test_register_families_are_not_typos(self):
        """18 of 19 "findings" on AFL++ were exactly this."""
        body = "\n".join(
            [
                'void f() { use("GUM_X86_EBX"); use("GUM_X86_RBX"); }',
                'void g() { use("ARM64_REG_W29"); use("ARM64_REG_X29"); }',
                'void h() { use("X86_INS_CMP"); use("X86_INS_JMP"); }',
            ]
        )
        rep = run({"a.c": body, "b.c": body.replace("void", "static void")})
        self.assertEqual(hard(rep), [])

    def test_insertion_family_is_not_a_typo(self):
        """`--disable-docs` against `--disable-bochs`: separate QEMU flags."""
        rep = run(
            {
                "build.sh": "./configure --disable-docs --disable-bochs --disable-vnc-png\n",
                "docs/x.md": "We pass `--disable-docs` and `--disable-bochs`.\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.not_typo_shaped)

    def test_digit_difference(self):
        rep = run({"a.c": 'x("NET_PROTO_IPV4"); y("NET_PROTO_IPV6");\n', "d.md": "`NET_PROTO_IPV4` and `NET_PROTO_IPV6`\n"})
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.digit_only)

    def test_antonym_segment(self):
        rep = run(
            {
                "a.c": 'x("SERVICE_MODE_ENABLE");\n',
                "b.c": 'y("SERVICE_MODE_DISABLE");\n',
                "d.md": "`SERVICE_MODE_ENABLE` and `SERVICE_MODE_DISABLE`\n",
            }
        )
        self.assertEqual(hard(rep), [])

    def test_nested_name(self):
        rep = run({"a.c": 'x("FOO_BAR_TIMEOUT");\n', "d.md": "`FOO_BAR_TIMEOUT_MS`\n"})
        self.assertEqual(hard(rep), [])

    def test_both_spellings_common_is_not_a_typo(self):
        many_a = "\n".join('x("SOME_LONG_MARKER");' for _ in range(12))
        many_b = "\n".join('y("SOME_LONG_MARKEP");' for _ in range(12))
        rep = run({"a.c": many_a, "b.c": many_b, "d.md": "`SOME_LONG_MARKER` `SOME_LONG_MARKEP`\n"})
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.both_common)


class TestUserFacing(unittest.TestCase):
    def test_identifier_only_spelling_is_soft(self):
        """`AFL_DEFER_FORKSVR` was a C variable name. The mismatch is real and
        invisible to users, so a human decides rather than the tool."""
        rep = run(
            {
                "docs/x.md": "Set `SOME_DEFER_FORKSRV` to enable.\n",
                "src/a.c": "static const char SOME_DEFER_FORKSVR[] = \"marker\";\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertEqual(len(rep.findings), 1)
        self.assertTrue(rep.not_user_facing)

    def test_both_in_strings_is_hard(self):
        rep = run(
            {
                "src/a.c": 'getenv("SOME_DEFER_FORKSRV");\n',
                "src/b.c": 'getenv("SOME_DEFER_FORKSVR");\n',
            }
        )
        self.assertEqual(len(hard(rep)), 1)


class TestShapes(unittest.TestCase):
    def test_shape_detection(self):
        self.assertEqual(nd.shape_of("AFL_ONLY_FSRV", "AFL_ONLY_FRSV"), "transposition")
        self.assertEqual(nd.shape_of("AFL_MARKER_ALPHA", "AFL_MARKER_ALPHB"), "substitution")
        self.assertIsNone(nd.shape_of("GUM_X86_EBX", "GUM_X86_RBX"))  # segment too short
        self.assertIsNone(nd.shape_of("A_B_ATTRIBUTE", "A_B_C_ATTRIBUTE"))  # insertion

    def test_bounded_distance(self):
        self.assertEqual(nd.bounded_distance("abcd", "abcd"), 0)
        self.assertEqual(nd.bounded_distance("abcd", "abce"), 1)
        self.assertGreater(nd.bounded_distance("abcd", "wxyz"), nd.MAX_DIST)


@unittest.skipUnless(HAS_AFLPP, "no AFL++ clone")
class TestOnRealAflpp(unittest.TestCase):
    """Negative control on a live project: after our fix #2865 there should be
    no hard findings there."""

    @classmethod
    def setUpClass(cls):
        cls.rep = nd.Report()
        nd.analyse(AFLPP, cls.rep)

    def test_no_hard_findings_after_the_fix(self):
        self.assertEqual(
            [f"{f.a}/{f.b}" for f in cls_hard(self.rep)], [], "a hard finding appeared"
        )

    def test_the_historical_pair_is_still_seen_as_a_pair(self):
        """The tool has to see the pair while not calling it a defect."""
        pairs = {frozenset((f.a, f.b)) for f in self.rep.findings}
        self.assertIn(
            frozenset(("AFL_GCC_ONLY_FSRV", "AFL_GCC_ONLY_FRSV")),
            pairs,
            "the historical case stopped being recognised at all",
        )

    def test_work_was_actually_done(self):
        self.assertGreater(self.rep.files, 500)
        self.assertGreater(self.rep.pairs_considered, 10000)


def cls_hard(rep):
    return [f for f in rep.findings if f.hard]


if __name__ == "__main__":
    unittest.main(verbosity=2)
