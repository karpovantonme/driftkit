#!/usr/bin/env python3
"""Tests for gitdrift.py: a field was added, the walker was not updated.

Two classes of lie the tool burned on during development:

  35 candidates. Any function touching 70% of the fields counted as a "walker",
     which swept in ordinary methods such as `Run`. Cured by requiring mirrored
     assignment: `out.Foo = in.Foo`.

  sibling structs. In karmada `ProviderInfo`, `RegionInfo` and `ZoneInfo` share
     five field names. A function about one of them produced findings in all
     three. Cured by requiring the function to name the type itself.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gitdrift as gd  # noqa: E402
import gosym  # noqa: E402


def scan(src: str) -> gd.Report:
    td = tempfile.mkdtemp()
    p = os.path.join(td, "a.go")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(src)
    rep = gd.Report()
    rep.findings = gd.discover(td, rep)
    return rep


SPEC = """package p

type Spec struct {
\tName     string
\tReplicas int32
\tImage    string
\tLabels   map[string]string
\tPaused   bool
\tWeight   int64
}
"""


def copier(missing: str = "") -> str:
    lines = []
    for f in ("Name", "Replicas", "Image", "Labels", "Paused", "Weight"):
        if f == missing:
            continue
        lines.append(f"\tout.{f} = in.{f}")
    return "func CopySpec(in *Spec) *Spec {\n\tout := &Spec{}\n" + "\n".join(lines) + "\n\treturn out\n}\n"


class TestFindsRealDrift(unittest.TestCase):
    def test_one_field_not_copied(self):
        rep = scan(SPEC + "\n" + copier(missing="Weight"))
        self.assertEqual([(f.struct, f.field, f.func) for f in rep.findings], [("Spec", "Weight", "CopySpec")])

    def test_full_copier_is_silent(self):
        rep = scan(SPEC + "\n" + copier())
        self.assertEqual(rep.findings, [])
        self.assertEqual(rep.walkers, 1)

    def test_comparison_style_walker(self):
        cmp_ = (
            "func EqualSpec(a, b *Spec) bool {\n"
            "\treturn a.Name == b.Name && a.Replicas == b.Replicas && "
            "a.Image == b.Image && a.Labels == nil && a.Paused == b.Paused\n}\n"
        )
        rep = scan(SPEC + "\n" + cmp_)
        self.assertEqual([f.field for f in rep.findings], ["Weight"])

    def test_coordinates_point_at_the_field_and_the_walker(self):
        rep = scan(SPEC + "\n" + copier(missing="Weight"))
        f = rep.findings[0]
        for ref in (f.struct_ref, f.field_ref, f.func_ref):
            self.assertRegex(ref, r"^.+a\.go:\d+$")
        self.assertNotEqual(f.field_ref, f.struct_ref)


class TestSilenceWhereItShould(unittest.TestCase):
    def test_ordinary_method_is_not_a_walker(self):
        """A method that uses fields is no walker. This produced 35 candidates,
        nearly all of them false."""
        run = (
            "func (s *Spec) Run() error {\n"
            "\tif s.Paused { return nil }\n"
            "\t_ = s.Name\n\t_ = s.Replicas\n\t_ = s.Image\n\t_ = s.Labels\n"
            "\treturn nil\n}\n"
        )
        rep = scan(SPEC + "\n" + run)
        self.assertEqual(rep.findings, [])
        self.assertEqual(rep.walkers, 0)

    def test_sister_structs_with_shared_field_names(self):
        """In karmada ProviderInfo, RegionInfo and ZoneInfo share fields.
        A function about one must not produce findings in the others."""
        src = """package p

type ProviderInfo struct {
\tName              string
\tScore             int64
\tAvailableReplicas int64
\tZones             map[string]struct{}
\tClusters          []int
\tExtra             string
}

type RegionInfo struct {
\tName              string
\tScore             int64
\tAvailableReplicas int64
\tZones             map[string]struct{}
\tClusters          []int
}

func generateRegionInfo(in *RegionInfo) *RegionInfo {
\tout := &RegionInfo{}
\tout.Name = in.Name
\tout.Score = in.Score
\tout.AvailableReplicas = in.AvailableReplicas
\tout.Zones = in.Zones
\tout.Clusters = in.Clusters
\treturn out
}
"""
        rep = scan(src)
        self.assertEqual(rep.findings, [], [f"{f.struct}.{f.field}" for f in rep.findings])
        self.assertTrue(any("ProviderInfo" in x for x in rep.type_not_named))

    def test_small_struct_is_not_judged(self):
        src = "package p\n\ntype T struct {\n\tA int\n\tB int\n\tC int\n}\n\n" + (
            "func Copy(in *T) *T {\n\tout := &T{}\n\tout.A = in.A\n\tout.B = in.B\n\treturn out\n}\n"
        )
        rep = scan(src)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.small_structs), 1)

    def test_fields_that_are_never_copied_by_design(self):
        src = (
            "package p\n\ntype S struct {\n\tName string\n\tA int\n\tB int\n\tC int\n\tD int\n"
            "\tmu sync.Mutex\n\tlogger Logger\n\tcache map[string]int\n}\n\n"
            "func CopyS(in *S) *S {\n\tout := &S{}\n\tout.Name = in.Name\n"
            "\tout.A = in.A\n\tout.B = in.B\n\tout.C = in.C\n\tout.D = in.D\n\treturn out\n}\n"
        )
        rep = scan(src)
        self.assertEqual(rep.findings, [])
        self.assertGreaterEqual(len(rep.skipped_fields), 3)

    def test_partial_walker_is_not_judged(self):
        """A function carrying a clear minority of the fields walks a part of the
        struct on purpose, and an omission there carries meaning."""
        src = SPEC + (
            "\nfunc CopyHead(in *Spec) *Spec {\n\tout := &Spec{}\n"
            "\tout.Name = in.Name\n\tout.Replicas = in.Replicas\n\tout.Image = in.Image\n\treturn out\n}\n"
        )
        rep = scan(src)
        self.assertEqual(rep.findings, [])

    def test_field_name_in_a_string_is_not_an_access(self):
        """A field name inside a string literal is no access."""
        src = SPEC + (
            "\nfunc CopySpec(in *Spec) *Spec {\n\tout := &Spec{}\n"
            + "\n".join(f"\tout.{f} = in.{f}" for f in ("Name", "Replicas", "Image", "Labels", "Paused"))
            + '\n\tlog("out.Weight = in.Weight")\n\treturn out\n}\n'
        )
        rep = scan(src)
        self.assertEqual([f.field for f in rep.findings], ["Weight"])


ENUM = """package p

type Kind int

const (
	KindAlpha Kind = iota
	KindBeta
	KindGamma
	KindDelta
)
"""


def switcher(cases, default=False) -> str:
    body = "".join(f'\tcase {c}:\n\t\treturn "{c}"\n' for c in cases)
    if default:
        body += '\tdefault:\n\t\treturn "?"\n'
    return f"func name(k Kind) string {{\n\tswitch k {{\n{body}\t}}\n\treturn \"\"\n}}\n"


class TestEnumShape(unittest.TestCase):
    """A value was added, the switch was not updated. The same shape as the
    Traefik case with knative, in Go: the condition was written before the
    capability existed."""

    def test_missing_case_without_default_is_a_finding(self):
        rep = scan(ENUM + "\n" + switcher(["KindAlpha", "KindBeta", "KindGamma"]))
        self.assertEqual([(f.struct, f.field, f.shape) for f in rep.findings],
                         [("Kind", "KindDelta", "enumeration")])

    def test_default_clause_means_the_omission_is_deliberate(self):
        """The same convention as the `exhaustive` linter."""
        rep = scan(ENUM + "\n" + switcher(["KindAlpha", "KindBeta", "KindGamma"], default=True))
        self.assertEqual(rep.findings, [])
        self.assertTrue(rep.has_default)

    def test_full_switch_is_silent(self):
        rep = scan(ENUM + "\n" + switcher(["KindAlpha", "KindBeta", "KindGamma", "KindDelta"]))
        self.assertEqual(rep.findings, [])

    def test_blank_identifier_is_not_a_member(self):
        """`_ Kind = iota` is no value. In otelcol those produced 12 candidates."""
        src = ("package p\n\ntype Kind int\n\nconst (\n\t_ Kind = iota\n"
               "\tKindAlpha\n\tKindBeta\n\tKindGamma\n)\n")
        rep = scan(src + "\n" + switcher(["KindAlpha", "KindBeta", "KindGamma"]))
        self.assertEqual(rep.findings, [])

    def test_sentinel_members_are_skipped(self):
        src = ("package p\n\ntype Kind int\n\nconst (\n\tKindAlpha Kind = iota\n"
               "\tKindBeta\n\tKindGamma\n\tKindUnknown\n\tKindMax\n)\n")
        rep = scan(src + "\n" + switcher(["KindAlpha", "KindBeta", "KindGamma"]))
        self.assertEqual(rep.findings, [])

    def test_generated_file_is_not_judged(self):
        """A patch to a generated file is pointless: the generator needs fixing."""
        src = ("// Code generated by mdatagen. DO NOT EDIT.\n\n" + ENUM + "\n"
               + switcher(["KindAlpha", "KindBeta", "KindGamma"]))
        rep = scan(src)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.generated), 1)

    def test_short_enum_is_not_judged(self):
        src = "package p\n\ntype K int\n\nconst (\n\tKA K = iota\n\tKB\n)\n"
        rep = scan(src + "\nfunc n(k K) string {\n\tswitch k {\n\tcase KA:\n\t\treturn \"a\"\n\t}\n\treturn \"\"\n}\n")
        self.assertEqual(rep.findings, [])

    def test_coordinates_point_at_the_member_and_the_switch(self):
        rep = scan(ENUM + "\n" + switcher(["KindAlpha", "KindBeta", "KindGamma"]))
        f = rep.findings[0]
        for ref in (f.struct_ref, f.field_ref, f.func_ref):
            self.assertRegex(ref, r"^.+a\.go:\d+$")


class TestStructFields(unittest.TestCase):
    def test_embedded_and_nested_are_handled(self):
        src = """package p

type S struct {
\tName string
\tsync.Mutex
\tNested struct {
\t\tInner int
\t}
\tA, B int
}
"""
        d = gosym.find(src, "S")
        self.assertEqual([n for n, _ in gosym.struct_fields(d)], ["Name", "Nested", "A", "B"])

    def test_line_numbers_are_absolute(self):
        d = gosym.find(SPEC, "Spec")
        fields = dict(gosym.struct_fields(d))
        self.assertEqual(SPEC.splitlines()[fields["Weight"] - 1].strip().split()[0], "Weight")


if __name__ == "__main__":
    unittest.main(verbosity=2)
