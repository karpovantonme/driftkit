#!/usr/bin/env python3
"""Tests for linkdrift.py: dead external links.

The network is never touched: probing is stubbed out. What gets tested is the
reason the tool exists, **reading the answer**, not the ability to make requests.

The lesson these tests were written for. The raw result on kotlin-web-site was
123 "not 200" while 47 were really dead. The whole difference is telling three
things apart:

  404, 410, 451    dead, that is a finding;
  403, 401, 429    not let in, the page is alive. The largest share of noise;
  timeout, reset   the fault may be ours, a second pass is needed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import linkdrift as ld  # noqa: E402


class FakeChecker(ld.Checker):
    """No network requests at all. Answers are prepared, attempts are counted."""

    def __init__(self, answers, flaky=()):
        self.answers = answers
        self.flaky = set(flaky)      # these answer only on the second attempt
        self.tries = {}
        self.requests = 0
        self.from_cache = 0
        self.offline = False
        self.cache_dir = tempfile.mkdtemp()

    def check(self, url):
        n = self.tries.get(url, 0) + 1
        self.tries[url] = n
        self.requests += 1
        if url in self.flaky and n == 1:
            return "net:timeout"
        return self.answers.get(url, "200")


def project(files: dict) -> str:
    root = tempfile.mkdtemp()
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


def run(files: dict, answers: dict, flaky=()) -> ld.Report:
    rep = ld.Report()
    ld.analyse(project(files), rep, FakeChecker(answers, flaky))
    return rep


class TestClassification(unittest.TestCase):
    def test_dead_codes(self):
        for code in ("404", "410", "451"):
            self.assertEqual(ld.classify(code), "dead", code)

    def test_blocked_codes_are_not_findings(self):
        """403 means bot protection while the page stays alive. The largest
        share of noise in the raw kotlin-web-site result."""
        for code in ("401", "403", "429", "503"):
            self.assertEqual(ld.classify(code), "blocked", code)

    def test_alive(self):
        for code in ("200", "204", "301", "302"):
            self.assertEqual(ld.classify(code), "alive", code)

    def test_network_error_is_not_a_verdict(self):
        self.assertEqual(ld.classify("net:timeout"), "unverified")


class TestFindsDeadLinks(unittest.TestCase):
    FILES = {"docs/a.md": "See [x](https://dead.example/x) and [y](https://live.example/y)\n"}

    def test_dead_link_is_a_finding(self):
        rep = run(self.FILES, {"https://dead.example/x": "404", "https://live.example/y": "200"})
        self.assertEqual([f.url for f in rep.findings], ["https://dead.example/x"])
        self.assertEqual(rep.alive, 1)

    def test_finding_carries_coordinates_and_count(self):
        rep = run(self.FILES, {"https://dead.example/x": "404"})
        f = rep.findings[0]
        self.assertEqual(f.count, 1)
        self.assertRegex(f.refs[0], r"^docs/a\.md:\d+$")
        self.assertEqual(f.status, "404")

    def test_blocked_link_is_counted_not_reported(self):
        rep = run(self.FILES, {"https://dead.example/x": "403", "https://live.example/y": "200"})
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.blocked), 1)


class TestSecondPass(unittest.TestCase):
    def test_flaky_link_is_retried_and_then_judged(self):
        """The failure may have been ours. Without a second pass the raw result
        on kotlin-web-site was 123 instead of 47."""
        files = {"a.md": "[x](https://flaky.example/x)\n"}
        rep = run(files, {"https://flaky.example/x": "404"}, flaky=["https://flaky.example/x"])
        self.assertEqual([f.url for f in rep.findings], ["https://flaky.example/x"])

    def test_link_that_never_answers_is_not_a_finding(self):
        files = {"a.md": "[x](https://gone.example/x)\n"}
        rep = run(files, {"https://gone.example/x": "net:timeout"})
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.unknown), 1)


class TestNoiseGuards(unittest.TestCase):
    def test_templated_url_is_skipped(self):
        files = {"a.md": "curl https://{{ .Values.host }}/api and https://<your-domain>/x\n"}
        rep = run(files, {})
        self.assertEqual(rep.findings, [])
        self.assertGreaterEqual(len(rep.templated), 1)

    def test_teaching_hosts_are_skipped(self):
        files = {"a.md": "http://localhost:8080/x https://example.com/y\n"}
        rep = run(files, {})
        self.assertEqual(rep.findings, [])
        self.assertGreaterEqual(len(rep.placeholder), 2)

    def test_internal_links_are_not_touched(self):
        """Internal links are their own species with their own traps: docs get
        assembled from several repositories."""
        files = {"a.md": "[x](/docs/x) and [y](../y.md)\n"}
        rep = run(files, {})
        self.assertEqual(rep.urls_found, 0)

    def test_one_dead_url_in_many_files_is_one_finding(self):
        """The kit-wide law: many occurrences of one trouble make one finding."""
        files = {f"docs/f{i}.md": "[x](https://dead.example/x)\n" for i in range(12)}
        rep = run(files, {"https://dead.example/x": "404"})
        self.assertEqual(len(rep.findings), 1)
        self.assertEqual(rep.findings[0].count, 12)
        self.assertEqual(len(rep.findings[0].refs), 3, "three coordinates are printed, the rest by count")

    def test_trailing_punctuation_is_trimmed(self):
        files = {"a.md": "See https://dead.example/x for details.\n"}
        rep = run(files, {"https://dead.example/x": "404"})
        self.assertEqual([f.url for f in rep.findings], ["https://dead.example/x"])


class TestOffline(unittest.TestCase):
    def test_offline_checks_nothing_and_says_so(self):
        rep = ld.Report()
        root = project({"a.md": "[x](https://dead.example/x)\n"})
        ld.analyse(root, rep, ld.Checker(tempfile.mkdtemp(), offline=True))
        self.assertEqual(rep.findings, [])
        self.assertEqual(len(rep.unknown), 1)
        self.assertIn("network disabled", rep.unknown[0])

    def test_limit_is_announced_not_silent(self):
        files = {"a.md": "\n".join(f"https://h{i}.example/x" for i in range(20))}
        rep = ld.Report()
        ld.analyse(project(files), rep, FakeChecker({}), limit=5)
        self.assertEqual(rep.urls_checked, 5)
        self.assertIn("first 5 checked", rep.truncated)


class TestReportedByUsers(unittest.TestCase):
    """Cases brought in by people running the tool. Each one is an issue."""

    def test_templated_stump_is_not_a_dead_link(self):
        """Issue #2: `}` is excluded from the address, so a templated URL
        arrives as a stump ending in `{tenant`, and that stump 404s honestly.
        Three false findings on poweradmin."""
        for url in (
            "https://login.microsoftonline.com/{tenant",
            "https://sts.windows.net/{tenant",
            "http://www.okta.com/{app-id",
        ):
            rep = ld.Report()
            self.assertFalse(ld.worth_checking(url, rep), url)
            self.assertIn(url, rep.templated)

    def test_illustrative_host_with_subdomain(self):
        """Issue #3: the host was anchored straight after the scheme, so
        `example.com` was caught and `www.example.com` was not. Ten false
        findings out of eleven on php-curl-class."""
        for url in (
            "https://www.example.com/image.png",
            "https://www.example.com/search",
            "http://api.example.org/v1",
            "https://example.com/plain",
        ):
            rep = ld.Report()
            self.assertFalse(ld.worth_checking(url, rep), url)
            self.assertIn(url, rep.placeholder)

    def test_identifier_uris_are_not_addresses(self):
        """Issue #1: SAML and XML namespaces are URIs by construction and were
        never meant to resolve. Eight false findings out of eleven on
        poweradmin."""
        for url in (
            "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
            "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
            "http://www.w3.org/2001/XMLSchema",
        ):
            rep = ld.Report()
            self.assertFalse(ld.worth_checking(url, rep), url)
            self.assertIn(url, rep.identifier)

    def test_a_real_dead_link_still_gets_through_the_guards(self):
        """The guards must not swallow everything: a plain address on a real
        host still reaches the checker."""
        rep = ld.Report()
        self.assertTrue(
            ld.worth_checking("https://bioinf.wehi.edu.au/featureCounts/", rep))



    # ------------------------------------------------------------------
    # Learned on canonical/ubuntu.com, 12.08.2026: 613 of 618 findings were
    # ours, not theirs. Four separate causes, one test each.
    # ------------------------------------------------------------------

    def _first(self, line):
        """The first address a line yields after normalisation."""
        for m in ld._URL.finditer(line):
            url = ld.normalise(m.group(0))
            if url is not None:
                return url
        return None

    def test_escaped_content_does_not_leak_into_the_address(self):
        """A recorded HTTP body keeps the page inside an escaped string. The
        backslash and the pipe end the address instead of joining it."""
        self.assertEqual(
            self._first("body: https://assets.ubuntu.com/v1/3c16f2f2-webinar.png|x"),
            "https://assets.ubuntu.com/v1/3c16f2f2-webinar.png")
        self.assertEqual(
            self._first('"https://assets.ubuntu.com/v1/3c16f2f2-webinar.png\\\\"'),
            "https://assets.ubuntu.com/v1/3c16f2f2-webinar.png")

    def test_a_bracket_in_the_file_name_survives(self):
        """Asset names carry parentheses and plus signs. Cutting at the
        opening bracket produced a stump that 404s honestly."""
        self.assertEqual(
            self._first("![x](https://assets.ubuntu.com/v1/02dc8e77-design%20(1).png)"),
            "https://assets.ubuntu.com/v1/02dc8e77-design%20(1).png")
        self.assertEqual(
            self._first("see https://assets.ubuntu.com/v1/17-banner+(13).png here"),
            "https://assets.ubuntu.com/v1/17-banner+(13).png")

    def test_a_markdown_link_still_loses_its_closing_bracket(self):
        """The other direction: an unpaired closing bracket belongs to the
        markdown around the address, not to the address."""
        self.assertEqual(
            self._first("[Read the docs](https://ubuntu.com/blog/thing)"),
            "https://ubuntu.com/blog/thing")

    def test_html_entities_are_decoded(self):
        """`&amp;` inside a query string used to cut the address in half,
        because the semicolon ended the match."""
        self.assertEqual(
            self._first("url: https://analytics.twitter.com/i/adsct?txn_id=l4pwm&amp;p_id=Twitter"),
            "https://analytics.twitter.com/i/adsct?txn_id=l4pwm&p_id=Twitter")

    def test_a_trailing_semicolon_is_still_dropped(self):
        """Letting the semicolon in must not swallow the one that ends a
        statement."""
        self.assertEqual(
            self._first("@import https://fonts.example.org/x.css; body {}"),
            "https://fonts.example.org/x.css")

    def test_an_address_broken_by_a_space_is_thrown_away(self):
        """An unpaired opening bracket means the real address continued past
        a space. It cannot be repaired, so it is dropped rather than
        reported as dead."""
        self.assertIsNone(
            self._first("img https://assets.ubuntu.com/v1/0c-[Webinar Recap].png"))

    def test_a_recorded_http_session_is_recognised_by_content(self):
        """VCR cassettes are not always kept in a directory we know by name."""
        self.assertTrue(ld._CASSETTE.search("interactions:\n- request:\n"))
        self.assertTrue(ld._CASSETTE.search("  recorded_with: VCR 4.0.2\n"))
        self.assertFalse(ld._CASSETTE.search("# interactions with the API\n"))


    def test_a_space_inside_an_attribute_does_not_cut_the_address(self):
        """66 of 72 findings on canonical/ubuntu.com were asset names with a
        real space in them, sitting inside an HTML attribute. A browser reads
        to the closing quote and encodes the spaces."""
        line = '<img src="https://assets.example.com/v1/hash-monitoring dashboard.png">'
        m = next(ld._URL.finditer(line))
        self.assertEqual(
            ld.normalise(ld.extend_to_quote(line, m)),
            "https://assets.example.com/v1/hash-monitoring%20dashboard.png")

    def test_a_quote_earlier_in_the_line_is_not_mistaken_for_an_attribute(self):
        """The address must not be stretched to a quote it does not belong to."""
        line = 'text "quoted" then https://example.org/a/b more words here'
        m = next(ld._URL.finditer(line))
        self.assertEqual(
            ld.normalise(ld.extend_to_quote(line, m)), "https://example.org/a/b")


if __name__ == "__main__":
    unittest.main(verbosity=2)
