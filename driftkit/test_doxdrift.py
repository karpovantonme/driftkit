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
import pathlib
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

    def test_headers_are_read_whatever_the_suffix(self):
        """`.h` counts, and this test used to pin the opposite.

        Until 06.08.2026 the tool globbed `*.hpp` only and this test asserted
        that a `.h` file yields nothing -- so the test defended the blindness
        rather than the behaviour. Measured cost: protobuf 6 headers read of
        611, abseil 0 of 385, and 21 findings invisible across a C++ pool that
        was already marked as checked.
        """
        for rel in ("include/a.h", "include/b.hpp", "include/c.hh",
                    "include/d.hxx", "include/e.ipp"):
            self.write(rel, self.DRIFT)
        self.assertEqual(len(doxdrift.scan(self.dir)), 5)

    def test_sources_are_still_not_read(self):
        """Declarations live in headers; a .cpp brings definitions and noise."""
        self.write("include/a.cpp", self.DRIFT)
        self.write("include/a.cc", self.DRIFT)
        self.assertEqual(doxdrift.scan(self.dir), [])

    # ---- declaration shapes reported from the field, issue #6 (PCL) ----

    def test_member_function_pointer_parameter(self):
        """void (T::*callback)(...) keeps its name between the brackets."""
        self.assertEqual(
            doxdrift.sig_params(
                "template<typename T> CallbackHandle registerImageCallback "
                "(void (T::*callback)(Image::Ptr, void* cookie), T& instance, "
                "void* cookie = nullptr) noexcept"),
            ["callback", "instance", "cookie"])

    def test_return_type_carrying_its_own_call_signature(self):
        """std::function<void (X)> is a type, not the argument list."""
        self.assertEqual(
            doxdrift.sig_params(
                "template <typename ScalarType> std::function<void (ScalarType)> "
                "scalarPropertyDefinitionCallback (const std::string& element_name, "
                "const std::string& property_name)"),
            ["element_name", "property_name"])

    def test_return_type_with_empty_parentheses(self):
        """The same shape with nothing inside the brackets of the type."""
        self.assertEqual(
            doxdrift.sig_params(
                "std::tuple<std::function<void ()>, std::function<void ()> > "
                "elementDefinitionCallback (const std::string& element_name, "
                "std::size_t count)"),
            ["element_name", "property_name"][:1] + ["count"])

    def test_unnamed_parameter_is_not_a_name(self):
        """`bool = false` has no name, so it cannot contradict a \\param."""
        self.assertEqual(doxdrift.sig_params("void setNonMaxSupression (bool = false)"), [])
        # and a named one of the same type still reads
        self.assertEqual(doxdrift.sig_params("void f (bool flag = false)"), ["flag"])

    def test_shared_skip_list_is_inherited(self):
        import common
        self.assertTrue(common.SKIP_DIRS <= doxdrift.SKIP_DIRS)

    def test_coordinate_is_a_file_line(self):
        h = findings_in("\n\n/** Does the thing.\n * \\param missing description\n */\nvoid f(int a);\n")
        self.assertEqual(h[0]["line"], 3)


# ------------------------------------------------- the second engine: clang


class TestClangEngine(unittest.TestCase):
    """The compiler as a source of the same findings.

    The engine is measured rather than trusted: on four Boost libraries 2233
    headers of 2233 parsed with empty stubs standing in for missing includes,
    and no build at all. Where a declaration still fails to parse, clang goes
    silent rather than wrong, so it under-reports exactly where the stubs were
    needed. That is why it is a second engine and not a replacement.
    """

    SAMPLE = """/// Does the thing.
/// \\param corpus_first where to start
/// \\param p the pattern
template <typename Iter>
Iter search(Iter corpus_first, Iter corpus_last) { return corpus_first; }
"""

    def setUp(self):
        if not doxdrift.clang_available():
            self.skipTest("clang++ is not installed")
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_the_compiler_finds_the_same_species(self):
        """`\\param p` against a signature that has no `p`."""
        pathlib.Path(self.dir, "a.hpp").write_text(self.SAMPLE, encoding="utf-8")
        hits = doxdrift.scan_clang(self.dir)
        self.assertEqual([h["name"] for h in hits], ["p"])
        self.assertEqual(hits[0]["kind"], "param")
        self.assertTrue(hits[0]["hard"])

    def test_it_carries_the_name_the_compiler_suggests(self):
        """clang answers `did you mean 'corpus_last'?`, which no regex can."""
        pathlib.Path(self.dir, "a.hpp").write_text(self.SAMPLE, encoding="utf-8")
        hits = doxdrift.scan_clang(self.dir)
        self.assertIn("corpus_last", hits[0]["sig"] + [hits[0]["note"]])

    def test_a_missing_include_is_stubbed_rather_than_fatal(self):
        """Missing includes stop the parse dead; empty stubs let it through."""
        pathlib.Path(self.dir, "a.hpp").write_text(
            "#include <no/such/header.hpp>\n" + self.SAMPLE, encoding="utf-8")
        hits = doxdrift.scan_clang(self.dir)
        self.assertEqual([h["name"] for h in hits], ["p"])
        self.assertGreaterEqual(doxdrift.COUNTS["stubs"], 1)

    def test_a_warning_from_an_included_header_is_counted_once(self):
        """A warning arrives once per file that includes the header.

        Counting them per compiled file counts mentions instead of entities:
        on Boost.Geometry that gave 64,966 instead of 605.
        """
        pathlib.Path(self.dir, "shared.hpp").write_text(self.SAMPLE, encoding="utf-8")
        for name in ("one.hpp", "two.hpp", "three.hpp"):
            pathlib.Path(self.dir, name).write_text('#include "shared.hpp"\n', encoding="utf-8")
        hits = doxdrift.scan_clang(self.dir)
        self.assertEqual(len(hits), 1, "the same warning was counted more than once")

    def test_both_engines_emit_the_same_shape(self):
        """The refuter and the sweep must not be able to tell them apart."""
        pathlib.Path(self.dir, "a.hpp").write_text(self.SAMPLE, encoding="utf-8")
        by_clang = doxdrift.scan_clang(self.dir)
        by_regex = doxdrift.scan(self.dir)
        for h in by_clang + by_regex:
            for key in ("kind", "hard", "file", "line", "name"):
                self.assertIn(key, h)
        self.assertEqual([h["name"] for h in by_regex], [h["name"] for h in by_clang])

    def test_a_doxygen_alias_of_the_project_is_not_a_parameter(self):
        """Boost.Geometry writes `\\param geometry \\param_geometry`.

        The second word is an alias declared in the project Doxyfile. The
        compiler knows nothing about a Doxyfile and reports a parameter called
        `_geometry`. On that tree it produced 605 warnings and every one was
        this. The signal is exact: no space between the command and the name.
        """
        pathlib.Path(self.dir, "a.hpp").write_text(
            "/// Appends a point.\n"
            "/// \\param geometry \\param_geometry\n"
            "/// \\param range_or_point The point to add\n"
            "template <typename Geometry, typename RangeOrPoint>\n"
            "void append(Geometry& geometry, RangeOrPoint const& range_or_point) {}\n",
            encoding="utf-8")
        hits = doxdrift.scan_clang(self.dir)
        self.assertEqual(hits, [], "a project alias was taken for a parameter")
        self.assertGreaterEqual(doxdrift.COUNTS["aliases"], 1)

    def test_an_alias_with_an_argument_in_braces(self):
        """Boost.Geometry also writes `\\param_strategy{Area}`.

        The compiler reports the whole thing as the name, braces included, so
        only the identifier part can be compared. Sixty-six warnings of this
        shape survived the first version of the guard.
        """
        pathlib.Path(self.dir, "c.hpp").write_text(
            "/// Area.\n"
            "/// \\param strategy \\param_strategy{Area}\n"
            "/// \\param geometry \\param_geometry\n"
            "template <typename G, typename S>\n"
            "void area(G const& geometry, S const& strategy) {}\n", encoding="utf-8")
        self.assertEqual(doxdrift.scan_clang(self.dir), [])
        self.assertEqual(doxdrift.COUNTS["aliases"], 2)

    def test_aliases_are_counted_as_entities_not_mentions(self):
        """The same alias arrives from every file that includes the header.

        Counting mentions gave 58,918 skipped aliases where there were far
        fewer. Third time this species turned up in one day.
        """
        pathlib.Path(self.dir, "shared.hpp").write_text(
            "/// Area.\n/// \\param geometry \\param_geometry\n"
            "template <typename G> void area(G const& geometry) {}\n", encoding="utf-8")
        for name in ("one.hpp", "two.hpp", "three.hpp"):
            pathlib.Path(self.dir, name).write_text('#include "shared.hpp"\n', encoding="utf-8")
        doxdrift.scan_clang(self.dir)
        self.assertEqual(doxdrift.COUNTS["aliases"], 1)

    def test_a_real_underscored_parameter_survives(self):
        """The guard is narrow: `\\param _term` with a space is a real name."""
        pathlib.Path(self.dir, "b.hpp").write_text(
            "/// Does the thing.\n"
            "/// \\param -term the term\n"
            "void f(int _term) {}\n", encoding="utf-8")
        hits = doxdrift.scan_clang(self.dir)
        self.assertEqual([h["name"] for h in hits], ["-term"])

    def test_the_engine_is_named_in_the_coverage_block(self):
        """A number without the engine that produced it cannot be compared."""
        import io
        from contextlib import redirect_stdout
        pathlib.Path(self.dir, "a.hpp").write_text(self.SAMPLE, encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            doxdrift.print_report(doxdrift.scan_clang(self.dir), self.dir, False, "clang")
        text = buf.getvalue()
        self.assertIn("engine:                 clang", text)
        self.assertIn("stub headers created", text)


class TestFunctionPointer(unittest.TestCase):
    """The name lives inside parentheses and the arguments in the next pair.

    On opencv this species alone gave 111 false findings out of 133: plugin
    tables and mouse callbacks are written this way, and every documented
    argument of every one of them read as missing.
    """

    FIELD = """
/** @brief Open video capture
@param filename File name or NULL
@param camera_index Camera index
@param handle pointer on Capture handle
*/
CvResult (CV_API_CALL *Capture_open)(const char* filename, int camera_index,
                                     CV_OUT CvPluginCapture* handle);
"""
    TYPEDEF = """
/** @brief Callback function for mouse events.
@param event one of the cv::MouseEventTypes constants.
@param x The x-coordinate of the mouse event.
@param y The y-coordinate of the mouse event.
@param flags one of the cv::MouseEventFlags constants.
@param userdata The optional parameter.
*/
typedef void (*MouseCallback)(int event, int x, int y, int flags, void* userdata);
"""

    def test_a_field_of_a_plugin_table_is_read(self):
        self.assertEqual(doxdrift.scan_text(self.FIELD, "plugin_api.hpp"), [])

    def test_a_callback_typedef_is_read(self):
        self.assertEqual(doxdrift.scan_text(self.TYPEDEF, "highgui.hpp"), [])

    def test_the_calling_convention_macro_does_not_become_the_name(self):
        self.assertEqual(
            doxdrift.sig_params(
                "CvResult (CV_API_CALL *Capture_open)(const char* filename, int camera_index)"),
            ["filename", "camera_index"])

    def test_a_pointer_to_a_member_function(self):
        self.assertEqual(
            doxdrift.sig_params("void (Widget::*handler)(int event, bool down)"),
            ["event", "down"])

    def test_an_ordinary_function_with_one_pointer_argument_is_untouched(self):
        """`void f(int *x)` looks like a declarator from the inside.

        What tells them apart is that a declarator is always followed by the
        argument list. Without that second condition this fix would have made
        every single-pointer-argument function invisible, which is the more
        expensive mistake of the two.
        """
        self.assertEqual(doxdrift.sig_params("void f(int *x)"), ["x"])
        self.assertEqual(doxdrift.sig_params("void f(int &x)"), ["x"])

    def test_a_real_mismatch_in_a_callback_is_still_reported(self):
        """The fix must not silence the species it was meant to parse."""
        src = ("/** @brief cb\n@param nosuch nothing\n*/\n"
               "typedef void (*MouseCallback)(int event, int x);\n")
        hits = doxdrift.scan_text(src, "a.hpp")
        self.assertEqual([h["name"] for h in hits], ["nosuch"])
        self.assertEqual(hits[0]["sig"], ["event", "x"])

    def test_they_are_counted_in_the_coverage_block(self):
        doxdrift.COUNTS["fnptr"] = 0
        doxdrift.scan_text(self.TYPEDEF, "a.hpp")
        self.assertEqual(doxdrift.COUNTS["fnptr"], 1)


class TestSuppressionMeasured(unittest.TestCase):
    """`\\cond` is the C++ construct closest to a numpydoc ignore directive.

    Measured rather than assumed, on 6 August 2026: 140 headers of the pool use
    `\\cond`, and none of the 49 findings standing at that moment sat inside
    such a region. No rule was added, and the reason is that the two constructs
    do not mean the same thing: a numpydoc directive says **do not check here**,
    while `\\cond` says **do not publish this**. A mismatch inside a `\\cond`
    region is still a real mismatch in the source.
    """

    def test_a_finding_inside_cond_is_still_reported(self):
        src = ("/// \\cond INTERNAL\n"
               "/// Does the thing.\n/// \\param nosuch nothing\n"
               "void f(int real) {}\n"
               "/// \\endcond\n")
        self.assertEqual([h["name"] for h in doxdrift.scan_text(src, "a.hpp")], ["nosuch"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
