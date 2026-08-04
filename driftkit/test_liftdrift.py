#!/usr/bin/env python3
"""Tests for liftdrift.py: copies that fell behind an upstream fix.

The cases these tests exist for:
  - upstream never touched the symbol -> stay quiet (negative control);
  - upstream evolved the symbol without fixing it (the signature gained a
    context) -> stay quiet. This is the trap that caught naive comparison:
    53 functions of 71 differed from master and almost all of it was evolution;
  - the commit message says fix while this symbol was never touched -> quiet;
  - the copy already holds the fix -> quiet;
  - the copy fell behind -> a finding, and only then.

Run: python3 test_liftdrift.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gosym  # noqa: E402
import liftdrift as ld  # noqa: E402

KARMADA_LIFTED = os.path.expanduser("~/Projects/oss/k8s/karmada/pkg/util/lifted")
HAS_KARMADA = os.path.isdir(KARMADA_LIFTED)


# --------------------------------------------------------------------------
# A stand-in GitHub: fully offline, not one network request
# --------------------------------------------------------------------------


class FakeGitHub(ld.GitHub):
    def __init__(self, files, commits=None, default_branch="master"):
        self.files = files  # {(repo, ref, path): text}
        self.commits = commits or {}  # {(repo, path): [commits]}
        self._dflt = default_branch
        self.calls = 0
        self.cache_hits = 0

    def raw_file(self, repo, path, ref):
        return self.files.get((repo, ref, path))

    def default_branch(self, repo):
        return self._dflt

    def branch_point_date(self, repo, ref):
        return "2022-12-08T00:00:00Z"

    def commits_for_path(self, repo, path, since, limit=300):
        return list(self.commits.get((repo, path), [])), False


def commit(sha, subject, parent):
    return {
        "sha": sha,
        "parents": [{"sha": parent}],
        "commit": {"message": subject, "committer": {"date": "2024-01-01T00:00:00Z"}},
    }


def write_copy(body: str, url: str, changed: bool = False) -> str:
    """Puts a copy carrying a lifted-code marker into a temporary directory."""
    td = tempfile.mkdtemp()
    p = os.path.join(td, "copy.go")
    mark = f"// +lifted:source={url}\n" + ("// +lifted:changed\n" if changed else "")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("package lifted\n\n" + mark + body + "\n")
    return td


def run(copy_dir, gh) -> ld.Report:
    borrows, unparsed, notes = ld.discover(copy_dir)
    rep = ld.Report(unparsed=unparsed, prose_notes=notes)
    ld.analyse(borrows, gh, rep)
    return rep


URL = "https://github.com/k/k/blob/release-1.26/pkg/x/y.go#L10-L20"
REPO, REF, PATH = "k/k", "release-1.26", "pkg/x/y.go"


def upstream(body: str) -> str:
    return "package y\n\nimport \"fmt\"\n\n" + body + "\n"


# --------------------------------------------------------------------------


class TestFixWordFilter(unittest.TestCase):
    """Without word boundaries `fix` turns up in prefix, suffix and fixture,
    and the filter stops filtering."""

    def test_real_fixes(self):
        for m in (
            "Fix panic when node is nil",
            "fix: data race in informer",
            "bugfix: memory leak in watcher",
            "Fix CVE-2023-1234 in proxy",
            "fix integer overflow in quota parsing",
            "resolve deadlock on shutdown",
        ):
            self.assertTrue(ld.looks_like_fix(m), m)

    def test_words_that_merely_contain_fix(self):
        for m in (
            "add prefix to generated names",
            "rename suffix handling",
            "add fixtures for scheduler tests",
            "make the field nilable in the API",
            "refactor: extract helper",
            "bump golang to 1.21",
        ):
            self.assertFalse(ld.looks_like_fix(m), m)

    def test_only_the_subject_line_counts(self):
        """The body of a k8s commit holds a release-note template, and anything matches it."""
        self.assertFalse(
            ld.looks_like_fix("add feature gate\n\n```release-note\nFixed nothing\n```")
        )


class TestDiscover(unittest.TestCase):
    def test_marker_forms(self):
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.go"), "w", encoding="utf-8") as fh:
            fh.write(
                "package p\n\n"
                f"// +lifted:source={URL}\n"
                "func One() {}\n\n"
                "// This code is lifted from the Kubernetes codebase.\n"
                "// https://github.com/k/k/blob/release-1.27/pkg/a/b.go#L1-L5\n"
                "func Two() {}\n\n"
                "// copied from https://github.com/k/k/blob/master/c.go\n"
                "func Three() {}\n\n"
                "// adapted from https://github.com/k/k/blob/master/d.go\n"
                "func Four() {}\n"
            )
        borrows, unparsed, _ = ld.discover(td)
        self.assertEqual(unparsed, [])
        self.assertEqual(sorted(b.decl.name for b in borrows), ["Four", "One", "Three", "Two"])

    def test_strict_marker_without_a_link_is_an_error(self):
        """+lifted:source= promises a link. No link means a broken parser."""
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.go"), "w", encoding="utf-8") as fh:
            fh.write("package p\n\n// +lifted:source=\nfunc One() {}\n")
        borrows, unparsed, notes = ld.discover(td)
        self.assertEqual(borrows, [])
        self.assertEqual(len(unparsed), 1)
        self.assertIn("a.go:3", unparsed[0])
        self.assertEqual(notes, [])

    def test_prose_mention_without_a_link_is_a_note_not_a_loss(self):
        """"This code is lifted from the Kubernetes codebase" in a file header is
        an explanation for a reader and carries no link by design. Counting it as
        a lost marker raises a false alarm: karmada has 13 of them."""
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.go"), "w", encoding="utf-8") as fh:
            fh.write(
                "package p\n\n"
                "// This code is lifted from the Kubernetes codebase.\n"
                "// However the code has been revised for using Lister.\n\n"
                "func One() {}\n"
            )
        borrows, unparsed, notes = ld.discover(td)
        self.assertEqual(borrows, [])
        self.assertEqual(unparsed, [])
        self.assertEqual(len(notes), 1)

    def test_marker_with_nothing_below_is_reported(self):
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.go"), "w", encoding="utf-8") as fh:
            fh.write(f"package p\n\nfunc One() {{}}\n\n// +lifted:source={URL}\n")
        borrows, unparsed, _ = ld.discover(td)
        self.assertEqual(borrows, [])
        self.assertEqual(len(unparsed), 1)
        self.assertIn("no declaration below", unparsed[0])

    def test_one_declaration_marked_twice_is_one_borrowing(self):
        """In karmada a declaration is often marked twice: by a file header with
        a link and by its own +lifted:source. Counting both doubles the borrows
        and the findings: 161 instead of 123 on live karmada."""
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.go"), "w", encoding="utf-8") as fh:
            fh.write(
                "package p\n\n"
                "// This code is lifted from the Kubernetes codebase.\n"
                f"// {URL}\n\n"
                f"// +lifted:source={URL}\n"
                "// +lifted:changed\n"
                "func One() {}\n"
            )
        borrows, unparsed, _ = ld.discover(td)
        self.assertEqual(len(borrows), 1)
        self.assertTrue(borrows[0].changed, "the changed mark was lost while merging")

    def test_link_that_is_not_a_file_is_a_note_not_an_error(self):
        """A link to a commit or an issue gives nothing to compare against:
        neither a file nor a version. thanos and argo-cd have those."""
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.go"), "w", encoding="utf-8") as fh:
            fh.write(
                "package p\n\n"
                "// copied from https://github.com/minio/minio-go/commit/008c7aa7\n"
                "func One() {}\n"
            )
        borrows, unparsed, notes = ld.discover(td)
        self.assertEqual(borrows, [])
        self.assertEqual(unparsed, [])
        self.assertEqual(len(notes), 1)

    def test_note_inside_a_function_body_is_not_a_symbol_marker(self):
        """"The constant 2 is copied from the logic over there" in the middle of
        a function body is an inline note rather than a marker of a lifted
        declaration."""
        td = tempfile.mkdtemp()
        with open(os.path.join(td, "a.go"), "w", encoding="utf-8") as fh:
            fh.write(
                "package p\n\n"
                "func One() {\n"
                f"\t// The constant 2 is copied from the logic in {URL}\n"
                "\tx := 2\n"
                "\t_ = x\n"
                "}\n"
            )
        borrows, unparsed, notes = ld.discover(td)
        self.assertEqual(borrows, [])
        self.assertEqual(unparsed, [])
        self.assertEqual(len(notes), 1)

    def test_changed_flag_is_read(self):
        td = write_copy("func F() {}", URL, changed=True)
        borrows, _, _ = ld.discover(td)
        self.assertTrue(borrows[0].changed)


class TestSilenceWhenNothingIsWrong(unittest.TestCase):
    def test_upstream_never_touched_the_symbol(self):
        """Negative control: a borrow upstream never changed."""
        body = "func F(a int) bool {\n\treturn a > 0\n}"
        gh = FakeGitHub({(REPO, REF, PATH): upstream(body), (REPO, "master", PATH): upstream(body)})
        rep = run(write_copy(body, URL), gh)
        self.assertEqual(rep.findings, [])
        self.assertEqual(rep.upstream_untouched, ["F (copy.go:4)"])

    def test_upstream_evolved_without_fixing_anything(self):
        """The trap: GetDeletableResources gained a context and a second return
        value. None of that bothers the copy."""
        old = "func GetDeletableResources(c Client) map[string]struct{} {\n\treturn nil\n}"
        new = (
            "func GetDeletableResources(ctx context.Context, c Client) "
            "(map[string]struct{}, error) {\n\treturn nil, nil\n}"
        )
        gh = FakeGitHub(
            {(REPO, REF, PATH): upstream(old), (REPO, "master", PATH): upstream(new)},
            {(REPO, PATH): [commit("aaa", "add context to garbage collector", "par")]},
        )
        rep = run(write_copy(old, URL), gh)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.no_fix_commits), 1)

    def test_fix_commit_that_did_not_touch_this_symbol(self):
        """A single k8s commit holds a dozen functions. The word fix in a message
        proves nothing: the body of the symbol gets compared before and after."""
        old = "func F(a int) bool {\n\treturn a > 0\n}"
        new = "func F(a int) bool {\n\treturn a > 0 // tidied up\n}"
        gh = FakeGitHub(
            {
                (REPO, REF, PATH): upstream(old),
                (REPO, "master", PATH): upstream(new + "\n\nfunc G() {}"),
                (REPO, "par", PATH): upstream(old),
                (REPO, "aaa", PATH): upstream(old),  # F was not changed by this commit
            },
            {(REPO, PATH): [commit("aaa", "fix panic in G", "par")]},
        )
        rep = run(write_copy(old, URL), gh)
        self.assertEqual(rep.findings, [])

    def test_copy_already_has_the_fix(self):
        old = "func F(a int) bool {\n\treturn a > 0\n}"
        new = "func F(a int) bool {\n\tif a == 0 {\n\t\treturn false\n\t}\n\treturn a > 0\n}"
        gh = FakeGitHub(
            {
                (REPO, REF, PATH): upstream(old),
                (REPO, "master", PATH): upstream(new),
                (REPO, "par", PATH): upstream(old),
                (REPO, "aaa", PATH): upstream(new),
            },
            {(REPO, PATH): [commit("aaa", "fix nil deref for zero input", "par")]},
        )
        rep = run(write_copy(new, URL), gh)  # the copy already holds the fix
        self.assertEqual([f for f in rep.findings if f.confident], [])
        self.assertEqual(len(rep.already_patched), 1)

    def test_fix_of_code_the_copy_never_had(self):
        """Upstream fixed a line the copy does not have at all: the copy is older
        than the code that was fixed. That is no finding: on live
        karmada 3 of the first 6 "findings" looked exactly like this."""
        old = "func F(a int) int {\n\treturn a\n}"
        mid = "func F(a int) int {\n\tlog.Info(newVar)\n\treturn a\n}"
        new = "func F(a int) int {\n\tlog.Info(fmt.Sprintf(newVar))\n\treturn a\n}"
        gh = FakeGitHub(
            {
                (REPO, REF, PATH): upstream(old),
                (REPO, "master", PATH): upstream(new),
                (REPO, "par", PATH): upstream(mid),
                (REPO, "aaa", PATH): upstream(new),
            },
            {(REPO, PATH): [commit("aaa", "fix internal error when serializing newVar", "par")]},
        )
        rep = run(write_copy(old, URL), gh)
        self.assertEqual([f for f in rep.findings if f.confident], [])
        self.assertEqual(len(rep.no_such_code), 1)

    def test_symbol_vanished_upstream_is_reported_not_guessed(self):
        old = "func F(a int) bool {\n\treturn a > 0\n}"
        gh = FakeGitHub(
            {(REPO, REF, PATH): upstream(old), (REPO, "master", PATH): "package y\n"}
        )
        rep = run(write_copy(old, URL), gh)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.symbol_not_found), 1)


class TestFindsRealLag(unittest.TestCase):
    OLD = "func F(a []int) int {\n\ttotal := 0\n\tfor _, x := range a {\n\t\ttotal += x\n\t}\n\treturn total / len(a)\n}"
    NEW = (
        "func F(a []int) int {\n\tif len(a) == 0 {\n\t\treturn 0\n\t}\n\ttotal := 0\n"
        "\tfor _, x := range a {\n\t\ttotal += x\n\t}\n\treturn total / len(a)\n}"
    )

    def gh(self):
        return FakeGitHub(
            {
                (REPO, REF, PATH): upstream(self.OLD),
                (REPO, "master", PATH): upstream(self.NEW),
                (REPO, "par", PATH): upstream(self.OLD),
                (REPO, "aaa", PATH): upstream(self.NEW),
            },
            {(REPO, PATH): [commit("aaa", "fix division by zero on empty slice", "par")]},
        )

    def test_lagging_copy_is_found(self):
        rep = run(write_copy(self.OLD, URL), self.gh())
        conf = [f for f in rep.findings if f.confident]
        self.assertEqual(len(conf), 1)
        f = conf[0]
        self.assertEqual(f.symbol, "F")
        self.assertIn("fix division by zero", f.commit_subject)
        self.assertEqual(f.added_in_copy, 0)
        # An ordinary fix adds a check and removes nothing. Requiring removed
        # lines would hide most of the real findings.
        self.assertEqual(f.removed_total, 0)
        self.assertGreaterEqual(f.overlap, ld.OVERLAP_MIN)

    def test_finding_carries_both_coordinates(self):
        f = [x for x in run(write_copy(self.OLD, URL), self.gh()).findings if x.confident][0]
        self.assertRegex(f.copy_ref, r"^.+copy\.go:\d+$")
        self.assertTrue(f.commit_url.startswith("https://github.com/k/k/commit/"))
        self.assertEqual(f.upstream_url, URL)

    def test_hand_edited_copy_is_downgraded_not_asserted(self):
        """A copy marked changed was edited locally, so claiming it fell behind
        is not ours to make. A human decides."""
        rep = run(write_copy(self.OLD, URL, changed=True), self.gh())
        self.assertEqual([f for f in rep.findings if f.confident], [])
        self.assertEqual(len(rep.findings), 1)

    def test_qualifier_rename_does_not_hide_the_lag(self):
        """Package names get rewritten when code is copied. The finding has to survive."""
        copy_body = self.OLD.replace("total += x", "total += pkg.Weigh(x)")
        gh = FakeGitHub(
            {
                (REPO, REF, PATH): upstream(self.OLD.replace("total += x", "total += other.Weigh(x)")),
                (REPO, "master", PATH): upstream(self.NEW),
                (REPO, "par", PATH): upstream(self.OLD.replace("total += x", "total += other.Weigh(x)")),
                (REPO, "aaa", PATH): upstream(self.NEW),
            },
            {(REPO, PATH): [commit("aaa", "fix division by zero on empty slice", "par")]},
        )
        rep = run(write_copy(copy_body, URL), gh)
        self.assertEqual(len([f for f in rep.findings if f.confident]), 1)

    def test_rewritten_copy_is_inconclusive_not_a_finding(self):
        """When a copy was rewritten beyond recognition, the absence of a fix in
        it proves nothing."""
        rep = run(write_copy("func F(a []int) int {\n\treturn 42\n}", URL), self.gh())
        self.assertEqual([f for f in rep.findings if f.confident], [])
        self.assertEqual(len(rep.inconclusive), 1)


@unittest.skipUnless(HAS_KARMADA, "no karmada clone")
class TestRenamedOnCopy(unittest.TestCase):
    """Names often change when code is copied: ValidateHorizontalPodAutoscaler
    became ValidateFederatedHPA. Searching by the name of the copy is useless,
    while the link carries a line range correct for the copied version."""

    UP = (
        "package y\n\n"                                  # 1-2
        "func Other() {}\n\n"                            # 3-4
        "func ValidateHorizontalPodAutoscaler(a []int) int {\n"  # 5
        "\treturn a[0]\n"                                # 6
        "}\n"                                            # 7
    )
    NEW = UP.replace("\treturn a[0]\n", "\tif len(a) == 0 {\n\t\treturn 0\n\t}\n\treturn a[0]\n")
    URL_R = "https://github.com/k/k/blob/release-1.26/pkg/x/y.go#L5-L7"

    def test_symbol_recovered_by_line_range(self):
        gh = FakeGitHub(
            {
                (REPO, REF, PATH): self.UP,
                (REPO, "master", PATH): self.NEW,
                (REPO, "par", PATH): self.UP,
                (REPO, "aaa", PATH): self.NEW,
            },
            {(REPO, PATH): [commit("aaa", "fix panic on empty slice", "par")]},
        )
        copy_body = "func ValidateFederatedHPA(a []int) int {\n\treturn a[0]\n}"
        rep = run(write_copy(copy_body, self.URL_R), gh)
        conf = [f for f in rep.findings if f.confident]
        self.assertEqual(len(conf), 1, rep.symbol_not_found)
        self.assertEqual(conf[0].symbol, "ValidateFederatedHPA")

    def test_ambiguous_line_range_is_not_guessed(self):
        """With two declarations equally close to the start of the range there is
        no guessing: the price of an error is an invented finding."""
        gh = FakeGitHub({(REPO, REF, PATH): self.UP, (REPO, "master", PATH): self.UP})
        url = "https://github.com/k/k/blob/release-1.26/pkg/x/y.go#L4-L8"
        rep = run(write_copy("func Renamed(a []int) int {\n\treturn a[0]\n}", url), gh)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.symbol_not_found), 1)


class TestMergeCommits(unittest.TestCase):
    def test_merge_commit_does_not_double_the_finding(self):
        """A merge commit carries the same diff as the real commit under it."""
        old = "func F(a []int) int {\n\treturn a[0]\n}"
        new = "func F(a []int) int {\n\tif len(a) == 0 {\n\t\treturn 0\n\t}\n\treturn a[0]\n}"
        merge = {
            "sha": "mmm",
            "parents": [{"sha": "par"}, {"sha": "aaa"}],
            "commit": {"message": "Merge pull request #1 from x/fix-panic", "committer": {"date": "2024-01-02T00:00:00Z"}},
        }
        gh = FakeGitHub(
            {
                (REPO, REF, PATH): upstream(old),
                (REPO, "master", PATH): upstream(new),
                (REPO, "par", PATH): upstream(old),
                (REPO, "aaa", PATH): upstream(new),
                (REPO, "mmm", PATH): upstream(new),
            },
            {(REPO, PATH): [merge, commit("aaa", "fix panic on empty slice", "par")]},
        )
        rep = run(write_copy(old, URL), gh)
        conf = [f for f in rep.findings if f.confident]
        self.assertEqual(len(conf), 1, [f.commit_subject for f in conf])
        self.assertNotIn("Merge", conf[0].commit_subject)


@unittest.skipUnless(HAS_KARMADA, "no karmada clone")
class TestOnRealKarmada(unittest.TestCase):
    def test_every_marker_is_parsed(self):
        borrows, unparsed, _ = ld.discover(KARMADA_LIFTED)
        self.assertGreater(len(borrows), 100, "suspiciously few borrows were found")
        self.assertEqual(unparsed, [], "a marker was lost silently")
        keys = [(b.copy_file, b.decl.name) for b in borrows]
        self.assertEqual(len(keys), len(set(keys)), "one declaration was counted twice")

    def test_registry_and_markers_agree_in_size(self):
        """doc.go is generated from the markers. A large divergence means the
        marker parser is blind to some form."""
        doc = os.path.join(KARMADA_LIFTED, "doc.go")
        rows = 0
        with open(doc, encoding="utf-8") as fh:
            for ln in fh:
                c = [x.strip() for x in ln.strip().strip("|").split("|")]
                if ln.startswith("|") and len(c) == 4 and not c[0].startswith("---") and c[0] != "lifted file":
                    rows += 1
        borrows, _, _ = ld.discover(KARMADA_LIFTED)
        self.assertGreaterEqual(len(borrows), rows)


@unittest.skipUnless(
    HAS_KARMADA and os.path.isdir(ld.DEFAULT_CACHE),
    "no karmada clone or a cold GitHub cache",
)
class TestKarmadaKnownTruth(unittest.TestCase):
    """A run over live karmada from cache, offline. Catches both directions:
    a lost signal and a burst of noise."""

    @classmethod
    def setUpClass(cls):
        gh = ld.GitHub(ld.DEFAULT_CACHE, offline=True)
        borrows, unparsed, notes = ld.discover(KARMADA_LIFTED)
        cls.rep = ld.Report(unparsed=unparsed, prose_notes=notes)
        ld.analyse(borrows, gh, cls.rep)
        cls.hard = [f for f in cls.rep.findings if f.confident]

    def test_negative_control_untouched_borrowings_are_silent(self):
        """Borrows upstream never touched. If the tool starts talking about
        them, it is inventing."""
        for name in (
            "IsNativeResource",
            "IsHugePageResourceName",
            "ValidateNonnegativeQuantity",
            "EqualIgnoreHash",
            "isNegativeErrorMsg",
        ):
            self.assertTrue(
                any(x.startswith(name + " (") for x in self.rep.upstream_untouched),
                f"{name} stopped counting as untouched",
            )
            self.assertNotIn(name, [f.symbol for f in self.rep.findings])

    def test_known_findings_survive(self):
        """Three findings verified by hand against the upstream commits."""
        want = {"GetDeletableResources", "SetFeatureGateDuringTest"}
        got = {f.symbol for f in self.hard}
        self.assertEqual(want - got, set(), f"lost: {sorted(want - got)}")

    def test_applied_fix_is_recognised_as_applied(self):
        """ValidateIngressLoadBalancerStatus was the third finding and it was
        fixed in the working tree (commit 32a6eb0, karmada pull request #7812).
        The tool has to notice that and stop talking about it. This is a stronger
        test than "the finding is still there": it is about going quiet."""
        self.assertNotIn("ValidateIngressLoadBalancerStatus", {f.symbol for f in self.hard})
        self.assertTrue(
            any("ValidateIngressLoadBalancerStatus" in x for x in self.rep.already_patched),
            "the fix is applied and the tool did not see it",
        )

    def test_noise_stays_low(self):
        """The first run gave 11 findings, 8 of which were the tool's own noise.
        A burst above ten is a reason to look for a bug rather than celebrate."""
        self.assertLessEqual(
            len(self.hard), 6, "\n".join(f"{f.symbol} {f.commit_subject}" for f in self.hard)
        )

    def test_every_finding_carries_both_coordinates(self):
        for f in self.rep.findings:
            self.assertRegex(f.copy_ref, r"^/.+\.go:\d+$")
            self.assertTrue(f.commit_url.startswith("https://github.com/"))
            self.assertTrue(f.upstream_url.startswith("https://github.com/"))

    def test_markers_are_not_double_counted(self):
        self.assertLessEqual(self.rep.borrows, 150, "borrows were counted twice")


if __name__ == "__main__":
    unittest.main(verbosity=2)
