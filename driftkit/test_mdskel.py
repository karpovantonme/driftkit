#!/usr/bin/env python3
"""Tests for mdskel.py: parsing the skeleton of a markdown page.

The known cases these were written for:
  - markup inside a code block is text, not markup ("# comment" in a Python
    example is no heading). Same family as a brace inside a string literal;
  - comments in code examples DO get translated, Kubernetes says so in its
    guidelines. Comparing blocks together with them declares the act of
    translation itself a mismatch;
  - a translation honestly points its links at its own locale, `/ja/docs/x`
    against `/docs/x`, and that is normal.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mdskel  # noqa: E402

OTEL = os.path.expanduser("~/Projects/oss/otel-io")
HAS_OTEL = os.path.isdir(os.path.join(OTEL, "content/en"))


class TestFences(unittest.TestCase):
    def test_markup_inside_a_code_block_is_not_markup(self):
        src = (
            "# Real heading\n\n"
            "```python\n"
            "# this line is a comment inside code\n"
            "x = [1](2)\n"
            "![not an image](not/a/path)\n"
            "```\n\n"
            "## Second real one\n"
        )
        sk = mdskel.parse(src)
        self.assertEqual([h[0] for h in sk.headings], [1, 2])
        self.assertEqual(sk.links, [])
        self.assertEqual(sk.images, [])
        self.assertEqual(len(sk.code_blocks), 1)
        self.assertEqual(sk.code_blocks[0][0], "python")

    def test_tilde_fence_and_long_fence(self):
        src = "~~~go\nfmt.Println()\n~~~\n\n````\nnested ```\n````\n"
        sk = mdskel.parse(src)
        self.assertEqual(len(sk.code_blocks), 2)
        self.assertEqual(sk.code_blocks[0][0], "go")

    def test_unclosed_fence_does_not_swallow_silently(self):
        src = "```yaml\nkey: value\n"
        sk = mdskel.parse(src)
        self.assertEqual(len(sk.code_blocks), 1)
        self.assertIn("key: value", sk.code_blocks[0][1])

    def test_line_numbers_are_real(self):
        src = "---\ntitle: x\n---\n\n# H\n\n```sh\nls\n```\n"
        sk = mdskel.parse(src)
        self.assertEqual(sk.headings[0][1], 5)
        self.assertEqual(sk.code_blocks[0][2], 7)


class TestFrontMatter(unittest.TestCase):
    def test_keys_are_read_and_body_starts_after(self):
        src = "---\ntitle: OpenTelemetry\ndefault_lang_commit: 5104763b\n---\n\n# H\n"
        sk = mdskel.parse(src)
        self.assertEqual(sk.front_matter["default_lang_commit"], "5104763b")
        self.assertEqual(len(sk.headings), 1)

    def test_no_front_matter_is_fine(self):
        sk = mdskel.parse("# H\n")
        self.assertEqual(sk.front_matter, {})
        self.assertEqual(len(sk.headings), 1)


class TestLinksAndImages(unittest.TestCase):
    def test_image_is_not_counted_as_a_link(self):
        sk = mdskel.parse("![alt](/img/a.png) and [text](/docs/b)\n")
        self.assertEqual([x[0] for x in sk.images], ["/img/a.png"])
        self.assertEqual([x[0] for x in sk.links], ["/docs/b"])

    def test_html_image(self):
        sk = mdskel.parse('<img src="/img/c.svg" alt="x">\n')
        self.assertEqual([x[0] for x in sk.images], ["/img/c.svg"])

    def test_link_with_title(self):
        sk = mdskel.parse('[a](https://example.com "title")\n')
        self.assertEqual([x[0] for x in sk.links], ["https://example.com"])


class TestNormalisation(unittest.TestCase):
    def test_translated_comments_do_not_break_code_equality(self):
        """Kubernetes has translating comments inside examples as a written rule."""
        en = "apiVersion: v1\n# the name of the pod\nkind: Pod"
        ru = "apiVersion: v1\n# pod name\nkind: Pod"
        self.assertEqual(mdskel.norm_code(en), mdskel.norm_code(ru))

    def test_trailing_comment_is_stripped_but_url_survives(self):
        """A trailing comment gets translated along with the prose, while `//`
        inside a URL must survive."""
        en = 'logger.Create<T>(); // this is equivalent to Create("T")'
        ja = "logger.Create<T>(); // これは Create(\"T\") と同等"
        self.assertEqual(mdskel.norm_code(en), mdskel.norm_code(ja))
        self.assertEqual(mdskel.norm_code("url = https://a.io/b"), "url = https://a.io/b")
        # the space after the marker is optional: originals often omit it
        self.assertEqual(
            mdskel.norm_code("'scope-name', //name (required)"),
            mdskel.norm_code("'scope-name', // 名前（必須）"),
        )

    def test_double_dash_is_a_flag_not_a_comment(self):
        """`docker run --rm` must survive, otherwise real differences get hidden."""
        self.assertEqual(mdskel.norm_code("docker run --rm image"), "docker run --rm image")

    def test_comment_marker_without_space_is_still_a_comment(self):
        self.assertEqual(mdskel.norm_code("//Make sure to pass ctx\nx := 1"), "x := 1")

    def test_real_code_difference_survives(self):
        a = "kind: Pod"
        b = "kind: Deployment"
        self.assertNotEqual(mdskel.norm_code(a), mdskel.norm_code(b))

    def test_locale_prefix_is_stripped(self):
        self.assertEqual(mdskel.norm_link("/ja/docs/x"), mdskel.norm_link("/docs/x"))
        self.assertEqual(mdskel.norm_link("/zh-CN/docs/x"), mdskel.norm_link("/docs/x"))

    def test_anchor_is_stripped_but_external_host_is_not(self):
        self.assertEqual(mdskel.norm_link("https://a.io/b#c"), "https://a.io/b")
        self.assertNotEqual(mdskel.norm_link("https://a.io/b"), mdskel.norm_link("/b"))

    def test_external_is_recognised(self):
        self.assertTrue(mdskel.is_external("https://x.io"))
        self.assertFalse(mdskel.is_external("/docs/x"))


@unittest.skipUnless(HAS_OTEL, "no opentelemetry.io clone")
class TestOnRealPages(unittest.TestCase):
    def test_skeleton_is_extracted_from_every_page_without_crashing(self):
        import glob

        files = glob.glob(os.path.join(OTEL, "content/en/**/*.md"), recursive=True)
        self.assertGreater(len(files), 300)
        empty = 0
        for p in files:
            with open(p, encoding="utf-8", errors="replace") as fh:
                sk = mdskel.parse(fh.read())
            if sk.size() == 0:
                empty += 1
        # pages with no heading, block or link at all exist but are rare;
        # if there are many, the parse is broken and the report still looks clean
        self.assertLess(empty / len(files), 0.15, f"empty skeletons: {empty} of {len(files)}")

    def test_translation_carries_the_source_commit(self):
        p = os.path.join(OTEL, "content/ja/_index.md")
        with open(p, encoding="utf-8") as fh:
            sk = mdskel.parse(fh.read())
        self.assertRegex(sk.front_matter.get("default_lang_commit", ""), r"^[0-9a-f]{7,40}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
