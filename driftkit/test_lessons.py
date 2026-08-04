#!/usr/bin/env python3
"""Tests for lessons.py: harvesting review replies. Run: python3 test_lessons.py"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lessons
from lessons import Lesson, Report


class FakeGitHub:
    """A stand-in GitHub: prepared answers, no network at all.

    The key is the start of a path. A value of `None` stands for "not in the
    cache and the network is disabled", the real `LookupError` from offline mode.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls = 0
        self.cache_hits = 0
        self.asked = []

    def api(self, path, paginate=False):
        self.asked.append(path)
        for prefix, val in self.routes.items():
            if path.startswith(prefix):
                if val is None:
                    raise LookupError(f"not in cache and network disabled: {path}")
                self.cache_hits += 1
                return val
        return []


def pr(repo="acme/thing", number=7, merged=None, state="open"):
    return {
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "state": state,
        "pull_request": {"merged_at": merged},
    }


def comment(body, login="maintainer", date="2026-08-01T10:00:00Z"):
    return {"body": body, "user": {"login": login}, "created_at": date}


# ------------------------------------------------------------ rejection language


class TestClassify(unittest.TestCase):
    def test_rejection_language(self):
        for body in (
            "Thanks, but this is not a bug - the value is clamped downstream.",
            "That's by design, see the comment above.",
            "It works as intended here.",
            "wontfix, sorry",
            "This is a false positive of your scanner.",
            "We do this on purpose to keep the old flag working.",
            "Intentionally left as is.",
        ):
            self.assertEqual(lessons.classify(body), "rejection", body)

    def test_rework_request(self):
        for body in (
            "Could you add a changelog entry?",
            "Please update the test to cover the new branch.",
            "Would you mind rebasing?",
            "needs a test",
            "Can you also sign-off the commit?",
        ):
            self.assertEqual(lessons.classify(body), "rework", body)

    def test_an_ordinary_reply_is_no_lesson(self):
        for body in (
            "Thanks for the contribution!",
            "LGTM",
            "Merging, thanks.",
            "I'll take a look next week.",
            "",
        ):
            self.assertIsNone(lessons.classify(body), body)

    def test_live_requests_from_a_real_run(self):
        """Word for word what maintainers wrote in our pull requests."""
        for body in (
            "Thanks. LGTM. Please fix the indentation and we should be good to go.",
            "Could you add a little `docs/changes/dev/14125.bugfix.rst` for this?",
            "Oof... could you add a tiny test that would have caught this?",
            "--dump-headers and --dump-bodies are deprecated - can you fix them in the same way please?",
            "Can you please add those to the env include file please: AFL_FRIDA_INST_NO_BACKPATCH",
        ):
            self.assertEqual(lessons.classify(body, "karpovantonme"), "rework", body)

    def test_the_word_changelog_without_a_request(self):
        """MontePy maintainers said the exact OPPOSITE of a request.

        The bare word "changelog" in the pattern caught two replies that lifted
        the changelog requirement. A name with no context around it.
        """
        for body in (
            "The changelog test failure can be ignored in this case.",
            "Looks good. I agree we can ignore the changelog requirement. Thanks!",
        ):
            self.assertIsNone(lessons.classify(body, "karpovantonme"), body)

    def test_a_negation_cancels_the_request(self):
        self.assertIsNone(
            lessons.classify("No need to add a test here, it's covered.", "karpovantonme")
        )
        self.assertIsNone(
            lessons.classify("Don't add a changelog entry for typo fixes.", "karpovantonme")
        )

    def test_a_command_to_a_bot_is_no_request(self):
        """sniffnet: «@all-contributors please add @karpovantonme for code».

        That is a command to a bot and a thank-you for a merged pull request.
        """
        self.assertIsNone(
            lessons.classify("@all-contributors please add @karpovantonme for code",
                             "karpovantonme")
        )

    def test_a_reply_addressed_to_us_stays_a_lesson(self):
        self.assertEqual(
            lessons.classify("@karpovantonme could you add a test?", "karpovantonme"),
            "rework",
        )

    def test_bots_are_recognised(self):
        for user in (
            {"login": "github-actions[bot]"},
            {"login": "codecov[bot]"},
            {"login": "dependabot[bot]"},
            {"login": "someone", "type": "Bot"},
        ):
            self.assertTrue(lessons.is_bot(user), user)
        for user in ({"login": "larsoner"}, {"login": "ncw", "type": "User"}):
            self.assertFalse(lessons.is_bot(user), user)

    def test_a_bot_template_never_becomes_a_lesson(self):
        """In nilearn a checklist from github-actions[bot] was taken for a request."""
        gh = FakeGitHub({
            "search/issues": [{"items": [pr()]}],
            "repos/": [comment("- [ ] Changelog entry in `doc/changes/latest.rst`",
                               "github-actions[bot]")],
        })
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.lessons, [])
        self.assertEqual(rep.prs_with_replies, 0)

    def test_rejection_outranks_rework(self):
        # One reply holding both: "not a defect, but add a test if you like".
        body = "This is by design. If you want, please add a test for the other case."
        self.assertEqual(lessons.classify(body), "rejection")

    def test_the_quote_spans_both_sides_of_the_phrase(self):
        body = "I looked at this and honestly it is by design, we clamp it later on purpose."
        q = lessons.quote_of(body, lessons.REJECTION)
        self.assertIn("by design", q)
        self.assertIn("looked at this", q)   # a piece from BEFORE the phrase
        self.assertIn("clamp it later", q)   # and from AFTER it

    def test_the_quote_is_one_line(self):
        q = lessons.quote_of("not a bug\n\nsee\n\nthe docs", lessons.REJECTION)
        self.assertNotIn("\n", q)


# ------------------------------------------------------------ walking pull requests


class TestAnalyse(unittest.TestCase):
    def routes(self, prs, comments):
        return {
            "search/issues": [{"items": prs}],
            "repos/": comments,
        }

    def test_a_rejection_becomes_a_lesson(self):
        gh = FakeGitHub(self.routes([pr()], [comment("Not a bug, by design.")]))
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.prs_seen, 1)
        self.assertEqual(rep.prs_with_replies, 1)
        # one reply arrives from three endpoints and has to appear once
        self.assertEqual(len(rep.lessons), 1)
        self.assertEqual(rep.lessons[0].kind, "rejection")
        self.assertEqual(rep.lessons[0].repo, "acme/thing")

    def test_our_own_replies_are_no_lesson(self):
        """Our own text is not a maintainer opinion."""
        gh = FakeGitHub(self.routes([pr()], [comment("this is not a bug?", "karpovantonme")]))
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.lessons, [])
        self.assertEqual(rep.prs_with_replies, 0)

    def test_login_case_does_not_matter(self):
        gh = FakeGitHub(self.routes([pr()], [comment("by design", "KarpovAntonMe")]))
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.lessons, [])

    def test_silence_is_no_rejection(self):
        """A pull request with no reply is absence of data.

        Of 48 submissions, 41 had been open for less than a day. Counting silence
        as rejection would make the kit learn rules from nothing.
        """
        gh = FakeGitHub(self.routes([pr(number=11)], []))
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.lessons, [])
        self.assertEqual(len(rep.silent), 1)
        self.assertIn("#11", rep.silent[0])

    def test_an_empty_reply_is_no_answer(self):
        gh = FakeGitHub(self.routes([pr()], [comment("   ")]))
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.prs_with_replies, 0)
        self.assertEqual(len(rep.silent), 1)

    def test_merged_outcome(self):
        gh = FakeGitHub(self.routes(
            [pr(merged="2026-08-01T00:00:00Z", state="closed")],
            [comment("Please add a changelog entry")],
        ))
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.lessons[0].state, "merged")

    def test_non_pull_requests_are_skipped(self):
        """A search/issues response also carries links to issues."""
        gh = FakeGitHub({
            "search/issues": [{"items": [{"html_url": "https://github.com/a/b/issues/3"}]}],
            "repos/": [],
        })
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.prs_seen, 0)

    def test_offline_with_empty_cache_does_not_crash(self):
        """An empty cache means zero lessons rather than a broken tool."""
        gh = FakeGitHub({"search/issues": None})
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.prs_seen, 0)
        self.assertEqual(rep.lessons, [])

    def test_offline_without_comments_does_not_crash(self):
        gh = FakeGitHub({"search/issues": [{"items": [pr()]}], "repos/": None})
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertEqual(rep.prs_seen, 1)
        self.assertEqual(len(rep.silent), 1)


# ----------------------------------------------------------------------- TSV


class TestTsv(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "lessons.tsv")

    def lesson(self, quote="by design", number=7):
        return Lesson(
            repo="acme/thing", number=number,
            url=f"https://github.com/acme/thing/pull/{number}",
            state="closed", author="maintainer", kind="rejection",
            quote=quote, date="2026-08-01",
        )

    def test_header_and_row(self):
        rep = Report(lessons=[self.lesson()])
        lessons.write_tsv(rep, self.path)
        lines = open(self.path, encoding="utf-8").read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("date\trepo"))
        self.assertEqual(len(lines[1].split("\t")), 9)

    def test_a_repeat_run_does_not_duplicate(self):
        """The tool gets run repeatedly and old lessons must not multiply."""
        lessons.write_tsv(Report(lessons=[self.lesson()]), self.path)
        lessons.write_tsv(Report(lessons=[self.lesson()]), self.path)
        self.assertEqual(len(open(self.path, encoding="utf-8").read().splitlines()), 2)

    def test_a_new_lesson_is_appended(self):
        lessons.write_tsv(Report(lessons=[self.lesson()]), self.path)
        lessons.write_tsv(Report(lessons=[self.lesson("wontfix", 9)]), self.path)
        self.assertEqual(len(open(self.path, encoding="utf-8").read().splitlines()), 3)

    def test_tabs_in_a_quote_do_not_break_columns(self):
        rep = Report(lessons=[self.lesson("this\tis\tby design")])
        lessons.write_tsv(rep, self.path)
        row = open(self.path, encoding="utf-8").read().splitlines()[1]
        self.assertEqual(len(row.split("\t")), 9)

    def test_a_rule_comes_only_from_a_human_hand(self):
        """The rule comes from the last column, filled in by a human.

        Deriving it from the quote is deliberately not done: a rule once invented
        from someone else's text produced 14,022 false findings.
        """
        lessons.write_tsv(Report(lessons=[self.lesson()]), self.path)
        self.assertEqual(lessons.rules_from_tsv(self.path), [])   # the column is empty

        text = open(self.path, encoding="utf-8").read()
        open(self.path, "w", encoding="utf-8").write(text.rstrip("\n") + "a section, not a flag\n")
        rules = lessons.rules_from_tsv(self.path)
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["pattern"], "a section, not a flag")
        self.assertIn("by design", rules[0]["why"])

    def test_no_file_means_no_rules(self):
        self.assertEqual(lessons.rules_from_tsv(os.path.join(self.dir, "missing.tsv")), [])


# ------------------------------------------------------------------ kit contract


class TestContract(unittest.TestCase):
    def test_json_matches_the_kit_contract(self):
        """The hardness field is called `hard`, like everywhere else.

        liftdrift once wrote `confident` while sweep read `hard` defaulting to
        true, and soft findings were counted as hard.
        """
        import json
        gh = FakeGitHub({
            "search/issues": [{"items": [pr()]}],
            "repos/": [comment("by design"), comment("please add a test")],
        })
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        d = tempfile.mkdtemp()
        out = os.path.join(d, "l.json")
        # going through main costs more: assemble what main assembles
        data = [{"hard": l.kind == "rejection", "kind": l.kind} for l in rep.lessons]
        json.dump(data, open(out, "w", encoding="utf-8"))
        loaded = json.load(open(out, encoding="utf-8"))
        self.assertTrue(any(x["hard"] for x in loaded))
        self.assertTrue(any(not x["hard"] for x in loaded))

    def test_exit_code(self):
        """Exit code 1 only on a rejection, a confirmed false finding."""
        gh = FakeGitHub({"search/issues": [{"items": [pr()]}],
                         "repos/": [comment("please add a changelog")]})
        rep = Report()
        lessons.analyse(gh, "karpovantonme", rep)
        self.assertFalse(any(l.kind == "rejection" for l in rep.lessons))


# ---------------------------------------------------- parsing paginated output


class TestPageParsing(unittest.TestCase):
    """Tests for `_decode_stream`, which is what broke the first live run.

    `gh api --paginate` concatenates pages without a separator. The old fix
    replaced "][" with a comma, which worked for arrays and broke on objects,
    and `search/issues` returns objects.
    """

    def test_concatenated_arrays(self):
        from liftdrift import _decode_stream
        self.assertEqual(_decode_stream('[{"a":1}][{"a":2}]'), [{"a": 1}, {"a": 2}])

    def test_concatenated_objects(self):
        from liftdrift import _decode_stream
        got = _decode_stream('{"items":[1,2]}{"items":[3]}')
        self.assertEqual(got, [{"items": [1, 2]}, {"items": [3]}])

    def test_a_single_value(self):
        from liftdrift import _decode_stream
        self.assertEqual(_decode_stream('{"a":1}'), [{"a": 1}])

    def test_newlines_between_pages(self):
        from liftdrift import _decode_stream
        self.assertEqual(_decode_stream('[1]\n\n[2]\n'), [1, 2])

    def test_trailing_garbage_does_not_crash(self):
        from liftdrift import _decode_stream
        self.assertEqual(_decode_stream('[1] not-json'), [1])

    def test_a_real_search_response_parses(self):
        """Exactly the shape that came back from live GitHub."""
        from liftdrift import _decode_stream
        raw = '{"total_count":2,"items":[{"html_url":"u1"}]}{"total_count":2,"items":[{"html_url":"u2"}]}'
        gh = FakeGitHub({"search/issues": _decode_stream(raw)})
        got = lessons.collect(gh, "kto")
        self.assertEqual([x["html_url"] for x in got], ["u1", "u2"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
