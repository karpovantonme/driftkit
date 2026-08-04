#!/usr/bin/env python3
"""Tests for doxdrift.py: Doxygen \\param against a C++ signature.

Run: python3 test_doxdrift.py

Every caveat below was written after a false finding on live Boost code, and
each test names the case it came from. The parse here is regex-based, because
parsing C++ properly needs a preprocessor and a compiler, so the caveats are
the whole precision story.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import doxdrift


def findings_in(text):
    return doxdrift.scan_text(text, "sample.hpp")


def names_in(text):
    return sorted(h["name"] for h in findings_in(text))


# ------------------------------------------------------------- simple cases


class TestBasics(unittest.TestCase):
    def test_documented_name_missing_from_signature(self):
        self.assertEqual(names_in('''
/** Does the thing.
 * \\param corpus_first where to start
 * \\param p the pattern
 */
template <typename Iter>
Iter search(Iter corpus_first, Iter corpus_last)
{
'''), ["p"])

    def test_matching_names_stay_quiet(self):
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param a first
 * \\param b second
 */
int f(int a, int b);
'''), [])

    def test_at_sign_instead_of_backslash(self):
        """Doxygen accepts both `@param` and `\\param`."""
        self.assertEqual(names_in('''
/** Does the thing.
 * @param missing no such argument
 */
int f(int a);
'''), ["missing"])

    def test_template_parameter(self):
        self.assertEqual(names_in('''
/** Does the thing.
 * \\tparam T the type
 */
template <class BufferSequence>
void f(BufferSequence& s);
'''), ["T"])

    def test_template_parameter_matches(self):
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\tparam T the type
 */
template <class T>
void f(T& s);
'''), [])

    def test_triple_slash_comment(self):
        self.assertEqual(names_in('''
/// Does the thing.
/// \\param missing description
void f(int a);
'''), ["missing"])

    def test_block_without_param_is_ignored(self):
        self.assertEqual(findings_in("/** Just a description. */\nvoid f(int a);\n"), [])


# ----------------------------------------------- caveats written on Boost


class TestCaveats(unittest.TestCase):
    def test_macro_in_the_return_type(self):
        """`BOOST_BEAST_ASYNC_RESULT2(Handler)` is no argument list."""
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param buffers the buffers
 */
template <class Handler>
BOOST_BEAST_ASYNC_RESULT2(Handler)
async_read(Handler&& buffers);
'''), [])

    def test_operator_parentheses(self):
        """`operator()` carries parentheses inside the name itself."""
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param x the value
 */
int operator()(int x) const;
'''), [])

    def test_decltype_is_not_an_argument_list(self):
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param x the value
 */
decltype(auto) f(int x);
'''), [])

    def test_array_by_reference(self):
        """`char(&dest)[N]` hides the name inside parentheses."""
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param dest destination
 */
template <std::size_t N>
void copy(char(&dest)[N]);
'''), [])

    def test_function_pointer(self):
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param cb the callback
 */
void on(void(*cb)(int));
'''), [])

    def test_preprocessor_directive_is_stripped(self):
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param a first
 */
#if defined(SOMETHING) && \\
    defined(OTHER)
void f(int a);
'''), [])

    def test_define_is_skipped(self):
        """A macro that assembles the whole signature cannot be parsed."""
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param x the value
 */
#define SOMETHING(x) f(x);
'''), [])

    def test_default_value_is_not_a_name(self):
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param n how many
 */
void f(int n = 10);
'''), [])

    def test_unnamed_argument_does_not_break_the_parse(self):
        self.assertEqual(names_in('''
/** Does the thing.
 * \\param missing description
 */
void f(int, double);
'''), ["missing"])


# ------------------------------------------- family of overloads (asio)


class TestOverloadFamily(unittest.TestCase):
    def test_a_repeated_name_dismisses_the_block(self):
        """asio documents a whole family of overloads in one comment.

        The block lists `ex`, `token`, `context`, `token`, `peer_endpoint`
        while a single declaration with one argument sits next to it. A
        repeated name proves the block does not describe one function.
        Twenty-five false findings on asio, all of this nature.
        """
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\param ex the executor
 * \\param token the completion token
 * \\param context the context
 * \\param token the completion token again
 * \\param peer_endpoint the endpoint
 */
socket accept(endpoint_type& peer_endpoint);
'''), [])

    def test_distinct_names_are_judged(self):
        """Without a repeat the block counts as describing one declaration."""
        self.assertEqual(names_in('''
/** Does the thing.
 * \\param corpus_first where to start
 * \\param corpus_last where to stop
 * \\param k_corpus_length the length
 */
Iter search(Iter corpus_first, Iter corpus_last);
'''), ["k_corpus_length"])

    def test_repeated_template_parameter(self):
        self.assertEqual(findings_in('''
/** Does the thing.
 * \\tparam T first
 * \\tparam T second
 */
template <class U>
void f(U u);
'''), [])


# ------------------------------------------------------------ kit contract


class TestContract(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    DRIFT = '''
/** Does the thing.
 * \\param missing description
 */
void f(int a);
'''

    def write(self, rel, text):
        full = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_json_carries_hard_and_exit_code(self):
        self.write("include/a.hpp", self.DRIFT)
        out = os.path.join(self.dir, "out.json")
        self.assertEqual(doxdrift.main([self.dir, "--json", out]), 1)
        data = json.load(open(out, encoding="utf-8"))
        self.assertTrue(data)
        self.assertTrue(all(x["hard"] for x in data))

    def test_clean_means_zero(self):
        self.write("include/a.hpp", "/** Does the thing. */\nvoid f(int a);\n")
        self.assertEqual(doxdrift.main([self.dir]), 0)

    def test_example_and_test_directories_are_skipped(self):
        for rel in ("test/a.hpp", "example/a.hpp", "doc/a.hpp", "extensions/a.hpp"):
            self.write(rel, self.DRIFT)
        self.assertEqual(doxdrift.scan(self.dir), [])

    def test_only_hpp_files_are_read(self):
        """Stated in the header: `.h`, `.cpp` and `.cc` are not read."""
        self.write("include/a.cpp", self.DRIFT)
        self.write("include/a.h", self.DRIFT)
        self.assertEqual(doxdrift.scan(self.dir), [])

    def test_shared_skip_list_is_inherited(self):
        import common
        self.assertTrue(common.SKIP_DIRS <= doxdrift.SKIP_DIRS)

    def test_coordinate_is_a_file_line(self):
        h = findings_in("\n\n/** Does the thing.\n * \\param missing description\n */\nvoid f(int a);\n")
        self.assertEqual(h[0]["line"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
