#!/usr/bin/env python3
"""Tests for gosym.py: cutting declarations out of Go.

Same order as everywhere in this kit: known cases first, trust afterwards.
Every test below is either a row from the "the tool lies in its own favour"
table carried over to Go parsing, or a live file from karmada.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gosym  # noqa: E402

KARMADA = os.path.expanduser("~/Projects/oss/k8s/karmada")
LIFTED = os.path.join(KARMADA, "pkg/util/lifted")
HAS_KARMADA = os.path.isdir(LIFTED)


class TestStripCode(unittest.TestCase):
    """A brace inside a literal must not count as a brace. This is the family
    of mistake that broke parsing of `map<K, V>`."""

    def test_braces_inside_strings_and_comments_are_neutralised(self):
        src = 'func F() {\n\ts := "}{"\n\t// }\n\t/* } */\n\tr := \'}\'\n\tb := `}\n}`\n}\n'
        clean = gosym.strip_code(src)
        self.assertEqual(len(clean.splitlines()), len(src.splitlines()))
        self.assertEqual(clean.count("{"), 1)
        self.assertEqual(clean.count("}"), 1)

    def test_escaped_quote_does_not_end_the_string(self):
        src = 'func F() {\n\ts := "a\\"}"\n}\n'
        self.assertEqual(gosym.strip_code(src).count("}"), 1)

    def test_line_count_is_preserved(self):
        src = "a\n/* x\ny\nz */\nb\n"
        self.assertEqual(
            len(gosym.strip_code(src).splitlines()), len(src.splitlines())
        )


class TestDeclarations(unittest.TestCase):
    SRC = """package p

import "fmt"

// comment
func Simple(a int) bool {
\tif a > 0 {
\t\treturn true
\t}
\treturn false
}

func (r *Recv) Method() error {
\treturn nil
}

const isNegativeErrorMsg = "must be non-negative"

var ValidatePodName = apimachineryvalidation.NameIsDNSSubdomain

var standardResources = sets.NewString(
\t"cpu",
\t"memory",
)

type ReplicaSetsByCreationTimestamp []*apps.ReplicaSet

type Big struct {
\tA int
\tB map[string]struct{ C int }
}

var (
\tgrouped1 = 1
\tgrouped2 = sets.NewString(
\t\t"x",
\t)
)
"""

    def decls(self):
        return {d.name: d for d in gosym.declarations(self.SRC)}

    def test_all_forms_are_seen(self):
        d = self.decls()
        self.assertEqual(
            sorted(d),
            sorted(
                [
                    "Simple",
                    "Method",
                    "isNegativeErrorMsg",
                    "ValidatePodName",
                    "standardResources",
                    "ReplicaSetsByCreationTimestamp",
                    "Big",
                    "grouped1",
                    "grouped2",
                ]
            ),
        )

    def test_function_ends_at_its_own_brace(self):
        d = self.decls()["Simple"]
        self.assertEqual(d.kind, "func")
        self.assertTrue(d.text.startswith("func Simple"))
        self.assertTrue(d.text.rstrip().endswith("}"))
        self.assertIn("return false", d.text)
        self.assertNotIn("Recv", d.text)

    def test_method_with_receiver(self):
        d = self.decls()["Method"]
        self.assertEqual(d.kind, "func")
        self.assertIn("Recv", d.text.splitlines()[0])

    def test_single_line_declarations_do_not_swallow_the_next(self):
        d = self.decls()
        self.assertEqual(len(d["isNegativeErrorMsg"].lines), 1)
        self.assertEqual(len(d["ValidatePodName"].lines), 1)

    def test_multiline_var_ends_at_closing_paren(self):
        d = self.decls()["standardResources"]
        self.assertEqual(len(d.lines), 4)
        self.assertTrue(d.text.rstrip().endswith(")"))

    def test_grouped_members_are_separate_declarations(self):
        d = self.decls()
        self.assertEqual(len(d["grouped1"].lines), 1)
        self.assertEqual(len(d["grouped2"].lines), 3)

    def test_line_numbers_are_real(self):
        d = self.decls()["Simple"]
        self.assertEqual(self.SRC.splitlines()[d.start - 1], "func Simple(a int) bool {")
        self.assertEqual(self.SRC.splitlines()[d.end - 1], "}")


class TestFindRefusesToGuess(unittest.TestCase):
    """The analogue of "searched by the short name instead of the full one":
    namesakes sharing a prefix must not be confused."""

    SRC = """package p

func IsStandardResourceName(s string) bool { return true }

func IsStandardQuotaResourceName(s string) bool { return false }
"""

    def test_prefix_collision_does_not_confuse(self):
        d = gosym.find(self.SRC, "IsStandardResourceName")
        self.assertIsNotNone(d)
        self.assertIn("return true", d.text)
        d2 = gosym.find(self.SRC, "IsStandardQuotaResourceName")
        self.assertIn("return false", d2.text)

    def test_mention_in_a_comment_is_not_a_declaration(self):
        src = "package p\n\n// FooBar does things\nfunc Other() {}\n"
        self.assertIsNone(gosym.find(src, "FooBar"))

    def test_duplicate_name_returns_nothing_rather_than_a_guess(self):
        src = "package p\n\nfunc F() { }\n\nfunc F() { }\n"
        self.assertIsNone(gosym.find(src, "F"))


class TestNextAfter(unittest.TestCase):
    def test_marker_binds_to_the_declaration_below_it(self):
        src = (
            "package p\n\n"
            "func Before() {}\n\n"
            "// +lifted:source=https://example/x\n"
            "// doc\n"
            "func Target(a int) {}\n\n"
            "func After() {}\n"
        )
        d = gosym.next_after(src, 5)
        self.assertEqual(d.name, "Target")


class TestNormalisation(unittest.TestCase):
    def test_indentation_and_comments_do_not_matter(self):
        a = "func F() {\n\treturn 1 // counter\n}"
        b = "func F() {\n        return  1\n}"
        self.assertTrue(gosym.bodies_equal(a, b))

    def test_package_qualifier_is_dropped(self):
        up = "func F() bool {\n\treturn helper.IsNativeResource(name)\n}"
        copy = "func F() bool {\n\treturn IsNativeResource(name)\n}"
        self.assertTrue(gosym.bodies_equal(up, copy))
        self.assertFalse(gosym.bodies_equal(up, copy, drop_qualifiers=False))

    def test_real_difference_survives_normalisation(self):
        a = "func F() bool {\n\treturn a > 0\n}"
        b = "func F() bool {\n\treturn a >= 0\n}"
        self.assertFalse(gosym.bodies_equal(a, b))


class TestParseBlobUrl(unittest.TestCase):
    """Six anchor forms occur in the live karmada tree. Missing any one of them
    means quietly losing a lifted declaration."""

    def test_every_anchor_form_in_the_wild(self):
        base = "https://github.com/kubernetes/kubernetes/blob/release-1.26/pkg/apis/core/helper/helpers.go"
        cases = {
            base + "#L57-L61": (57, 61),
            base + "#LL266-L276": (266, 276),
            base + "#L563C1-L595": (563, 595),
            base + "#L58": (58, 58),
            base + "#L6167-L6177": (6167, 6177),
            base + "#L536-561": (536, 561),
            base: None,
        }
        for url, want in cases.items():
            got = gosym.parse_github_blob(url)
            self.assertIsNotNone(got, url)
            repo, ref, path, rng = got
            self.assertEqual(repo, "kubernetes/kubernetes")
            self.assertEqual(ref, "release-1.26")
            self.assertEqual(path, "pkg/apis/core/helper/helpers.go")
            self.assertEqual(rng, want, url)

    def test_not_a_blob_url(self):
        self.assertIsNone(gosym.parse_github_blob("https://example.com/x.go"))


@unittest.skipUnless(HAS_KARMADA, "no karmada clone")
class TestOnRealKarmada(unittest.TestCase):
    def test_every_marker_binds_to_a_declaration(self):
        """If a marker fails to bind to a declaration, the tool loses that lifted
        symbol without a word and the report still looks clean."""
        missed = []
        total = 0
        for root, _dirs, names in os.walk(LIFTED):
            for n in sorted(names):
                if not n.endswith(".go"):
                    continue
                p = os.path.join(root, n)
                with open(p, encoding="utf-8") as fh:
                    src = fh.read()
                for i, ln in enumerate(src.splitlines(), 1):
                    if "+lifted:source=" not in ln:
                        continue
                    total += 1
                    if gosym.next_after(src, i) is None:
                        missed.append(f"{p}:{i}")
        self.assertGreater(total, 100, "suspiciously few markers found")
        self.assertEqual(missed, [])

    def test_known_symbols_are_extracted_whole(self):
        with open(os.path.join(LIFTED, "corehelpers.go"), encoding="utf-8") as fh:
            src = fh.read()
        d = gosym.find(src, "standardQuotaResources")
        self.assertIsNotNone(d)
        self.assertTrue(d.text.rstrip().endswith(")"))
        self.assertGreater(len(d.lines), 3)
        f = gosym.find(src, "IsStandardResourceName")
        self.assertEqual(f.kind, "func")
        self.assertIn("standardResources.Has", f.text)
        self.assertNotIn("integerResources", f.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
