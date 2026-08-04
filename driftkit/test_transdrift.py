#!/usr/bin/env python3
"""Tests for transdrift.py: translations that fell behind the original.

Every test below is a class of false positive removed on the live Japanese
translation of opentelemetry.io. The path was **170 candidates -> 9 -> 7 proven
by commit**. The numbers are written down so that nobody "simplifies" the
normalisation back.

The classes that produced the lies:
  166  the block is present and differs by a translated comment (full-line,
       trailing, with and without a space after the marker);
    3  a mermaid diagram: node labels are prose and get translated;
    2  a two-line block where one comment outweighs everything.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transdrift as td  # noqa: E402

OTEL = os.path.expanduser("~/Projects/oss/otel-io")
HAS_OTEL = os.path.isdir(os.path.join(OTEL, "content/ja"))


class FakeGitHub:
    """No network requests at all. Returns prepared commits."""

    def __init__(self, commits=None, patches=None):
        self.commits = commits or []
        self.patches = patches or {}
        self.calls = 0
        self.cache_hits = 0

    def api(self, path, paginate=False):
        if "/commits?" in path:
            return list(self.commits)
        if "/commits/" in path:
            sha = path.rsplit("/", 1)[-1]
            for c in self.commits:
                if c["sha"] == sha:
                    return dict(c, files=self.patches.get(sha, []))
            return {"commit": {"committer": {"date": "2020-01-01T00:00:00Z"}}}
        return {}


def commit(sha, subject, date="2025-06-01T00:00:00Z"):
    return {
        "sha": sha,
        "parents": [{"sha": "par"}],
        "commit": {"message": subject, "committer": {"date": date}},
    }


def scenario(original: str, translation: str, gh=None, repo=None):
    td_dir = tempfile.mkdtemp()
    o = os.path.join(td_dir, "en")
    t = os.path.join(td_dir, "ja")
    os.makedirs(o)
    os.makedirs(t)
    with open(os.path.join(o, "page.md"), "w", encoding="utf-8") as fh:
        fh.write(original)
    with open(os.path.join(t, "page.md"), "w", encoding="utf-8") as fh:
        fh.write(translation)
    rep = td.Report()
    td.analyse(o, t, repo, gh, rep, "content/en")
    return rep


FM = "---\ntitle: X\ndefault_lang_commit: abc1234\n---\n\n"
LONG = "\n".join(f"line{i}: value{i}" for i in range(8))


# --------------------------------------------------------------------------
# Stay quiet where there is no mismatch
# --------------------------------------------------------------------------


class TestSilence(unittest.TestCase):
    def test_identical_skeleton(self):
        page = FM + f"# H\n\n```yaml\n{LONG}\n```\n\n[a](https://x.io)\n"
        rep = scenario(page, page)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.in_sync), 1)

    def test_translated_line_comments(self):
        """The largest class of lie: 166 false of 170 on a live translation."""
        en = FM + "```yaml\n# the name of the pod\n" + LONG + "\n```\n"
        ja = FM + "```yaml\n# ポッドの名前\n" + LONG + "\n```\n"
        self.assertEqual(scenario(en, ja).findings, [])

    def test_translated_trailing_comments_with_and_without_space(self):
        """`//name` in the original and `// 名前` in the translation: one block."""
        en = FM + "```php\n" + "\n".join(
            [f"call{i}(); //comment {i}" for i in range(5)]
        ) + "\n```\n"
        ja = FM + "```php\n" + "\n".join(
            [f"call{i}(); // 説明 {i}" for i in range(5)]
        ) + "\n```\n"
        self.assertEqual(scenario(en, ja).findings, [])

    def test_mermaid_labels_are_prose(self):
        en = FM + "```mermaid\nflowchart LR;\n  A[MySQL client]-->B[Collector];\n  B-->C[Jaeger];\n```\n"
        ja = FM + "```mermaid\nflowchart LR;\n  A[MySQLクライアント]-->B[コレクター];\n  B-->C[イェーガー];\n```\n"
        self.assertEqual(scenario(en, ja).findings, [])

    def test_two_line_block_is_not_judged(self):
        """One translated comment outweighs a two-line block."""
        en = FM + "# H\n\n```go\n//Make sure to pass ctx\ncur, err := c.Find(ctx)\n```\n" + f"```yaml\n{LONG}\n```\n"
        ja = FM + "# H\n\n```go\n//ctx を渡すこと\ncur, err := c.Find(ctx)\n```\n" + f"```yaml\n{LONG}\n```\n"
        self.assertEqual(scenario(en, ja).findings, [])

    def test_localised_internal_link(self):
        en = FM + f"```yaml\n{LONG}\n```\n\n[a](/docs/x)\n"
        ja = FM + f"```yaml\n{LONG}\n```\n\n[a](/ja/docs/x)\n"
        self.assertEqual(scenario(en, ja).findings, [])

    def test_stub_translation_is_counted_not_reported(self):
        en = FM + f"# H\n\n```yaml\n{LONG}\n```\n" * 3
        ja = FM + "まだ翻訳されていません\n"
        rep = scenario(en, ja)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.stubs), 1)

    def test_heavily_incomplete_translation_is_one_line_not_many(self):
        """The lesson from the interface comparator: many losses in one place are
        one trouble. Printing N means lying in our own favour N times over."""
        blocks = "".join(
            f"```yaml\nblock{i}: a\nblock{i}: b\nblock{i}: c\n```\n\n" for i in range(6)
        )
        en = FM + "# H\n\n" + blocks
        ja = FM + "# H\n\n" + "```yaml\nblock0: a\nblock0: b\nblock0: c\n```\n\nテキスト\n" * 4
        rep = scenario(en, ja)
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.incomplete), 1)


# --------------------------------------------------------------------------
# Find the real thing
# --------------------------------------------------------------------------


class TestFoldByPage(unittest.TestCase):
    def test_many_gaps_on_one_page_collapse_into_one(self):
        """A page with 17 links missing from one table is one trouble. That is
        how the Chinese translation of supported-libraries.md came out."""
        # More than half of the skeleton has to match, otherwise the other rule
        # fires: "the translation is incomplete as a whole".
        kept = "\n".join(f"[lib{i}](https://example{i}.io/)" for i in range(14))
        gone = "\n".join(f"[new{i}](https://newlib{i}.io/)" for i in range(8))
        en = FM + f"# H\n\n```yaml\n{LONG}\n```\n\n" + kept + "\n" + gone + "\n"
        ja = FM + f"# H\n\n```yaml\n{LONG}\n```\n\n" + kept + "\n"
        rep = scenario(en, ja)
        self.assertEqual([f.kind for f in rep.findings], ["page-behind"])
        self.assertIn("8", rep.findings[0].message)

    def test_few_gaps_stay_separate(self):
        kept = "\n".join(f"[lib{i}](https://example{i}.io/)" for i in range(6))
        en = FM + f"```yaml\n{LONG}\n```\n\n" + kept + "\n[a](https://a.io/)\n[b](https://b.io/)\n"
        ja = FM + f"```yaml\n{LONG}\n```\n\n" + kept + "\n"
        rep = scenario(en, ja)
        self.assertEqual(len(rep.findings), 2)
        self.assertTrue(all(f.kind == "missing-link" for f in rep.findings))


class TestFindsRealDrift(unittest.TestCase):
    EN = FM + f"# H\n\n```yaml\n{LONG}\n```\n\n```yaml\nnew_section:\n  added: true\n  recently: yes\n```\n"
    JA = FM + f"# H\n\n```yaml\n{LONG}\n```\n"

    def test_missing_code_block(self):
        rep = scenario(self.EN, self.JA)
        self.assertEqual([f.kind for f in rep.findings], ["missing-code-block"])
        self.assertIn("page.md", rep.findings[0].page)

    def test_missing_external_link(self):
        en = FM + f"```yaml\n{LONG}\n```\n\n[k6](https://k6.io/)\n"
        ja = FM + f"```yaml\n{LONG}\n```\n"
        rep = scenario(en, ja)
        self.assertEqual([f.kind for f in rep.findings], ["missing-link"])
        self.assertIn("k6.io", rep.findings[0].message)

    def test_without_proof_finding_is_soft(self):
        rep = scenario(self.EN, self.JA)
        self.assertFalse(rep.findings[0].hard)
        self.assertEqual(len(rep.unproven), 1)

    def test_with_proof_finding_is_hard_and_carries_the_commit(self):
        gh = FakeGitHub(
            commits=[commit("deadbeef", "Update OBI docs for v0.10.0 (#10631)")],
            patches={"deadbeef": [{"filename": "content/en/page.md", "patch": "@@\n+  recently: yes\n"}]},
        )
        rep = scenario(self.EN, self.JA, gh=gh, repo="o/r")
        self.assertTrue(rep.findings[0].hard)
        self.assertIn("deadbeef", rep.findings[0].proof)
        self.assertTrue(any("Update OBI docs" in d for d in rep.findings[0].detail))

    def test_commit_not_found_downgrades_to_soft(self):
        """With no commit after the translation point the translator may have
        shortened it deliberately, so claiming drift is not allowed."""
        gh = FakeGitHub(commits=[commit("deadbeef", "unrelated change")], patches={"deadbeef": []})
        rep = scenario(self.EN, self.JA, gh=gh, repo="o/r")
        self.assertFalse(rep.findings[0].hard)

    def test_merge_commit_is_not_accepted_as_proof(self):
        merge = commit("mmm", "Merge pull request #1")
        merge["parents"] = [{"sha": "a"}, {"sha": "b"}]
        gh = FakeGitHub(
            commits=[merge],
            patches={"mmm": [{"filename": "content/en/page.md", "patch": "@@\n+  recently: yes\n"}]},
        )
        rep = scenario(self.EN, self.JA, gh=gh, repo="o/r")
        self.assertFalse(rep.findings[0].hard)


class TestAnchor(unittest.TestCase):
    def test_all_known_marker_names(self):
        import mdskel

        for key in td.SOURCE_KEYS:
            sk = mdskel.parse(f"---\n{key}: abc1234\n---\n\n# H\n")
            self.assertEqual(td.source_anchor(sk), "abc1234", key)

    def test_no_marker_is_recorded(self):
        en = FM + f"```yaml\n{LONG}\n```\n\n```yaml\nx: 1\ny: 2\nz: 3\n```\n"
        ja = "---\ntitle: X\n---\n\n" + f"```yaml\n{LONG}\n```\n"
        rep = scenario(en, ja)
        self.assertEqual(len(rep.no_anchor), 1)


@unittest.skipUnless(HAS_OTEL, "no opentelemetry.io clone")
class TestOnRealTranslation(unittest.TestCase):
    """Negative control on live material: 389 Japanese pages.
    If the share of pages with findings climbs, the normalisation broke."""

    @classmethod
    def setUpClass(cls):
        cls.rep = td.Report()
        td.analyse(
            os.path.join(OTEL, "content/en"),
            os.path.join(OTEL, "content/ja"),
            None,
            None,
            cls.rep,
            "content/en",
        )

    def test_most_pages_are_in_sync(self):
        self.assertGreater(self.rep.pairs, 300)
        share = len(self.rep.in_sync) / self.rep.pairs
        self.assertGreater(share, 0.85, f"only {share:.0%} in sync, the normalisation broke")

    def test_candidate_count_stays_low(self):
        """The first run gave 170 candidates, 166 of which were normalisation
        noise. Going back to dozens is a reason to look for a bug."""
        self.assertLessEqual(len(self.rep.findings), 12, "\n".join(
            f"{f.page}: {f.message}" for f in self.rep.findings
        ))

    def test_known_real_findings_survive(self):
        """Verified by hand: the Japanese translation of these pages really does
        lack the corresponding block or link."""
        pages = {f.page for f in self.rep.findings}
        for want in (
            "docs/demo/architecture.md",
            "docs/platforms/kubernetes/operator/_index.md",
            "docs/zero-code/obi/configure/export-data.md",
        ):
            self.assertIn(want, pages, f"a verified finding was lost: {want}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
