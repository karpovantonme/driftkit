#!/usr/bin/env python3
"""test_paramdrift.py: the JSDoc engine, and the shapes that make it wrong.

Two thirds of these cases exist because of a false positive somebody would
have received in a pull request. They are written as tests rather than as
comments so that a later change cannot quietly bring the noise back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paramdrift  # noqa: E402


def findings(src: str, name: str = "a.js"):
    report = paramdrift.Report()
    lang = paramdrift.EXT_TO_LANG[os.path.splitext(name)[1]]
    paramdrift.scan_text(src, name, lang, report)
    return report


def names(src: str, name: str = "a.js"):
    return [f["documented"] for f in findings(src, name).findings]


class TestDocComment(unittest.TestCase):
    def test_plain_tag(self):
        self.assertEqual(paramdrift.js_doc_params(" @param count how many "),
                         [("count", 1)])

    def test_type_before_the_name(self):
        """JSDoc puts the type first, the opposite way round from Doxygen."""
        got = paramdrift.js_doc_params(" @param {string} count how many ")
        self.assertEqual([n for n, _ in got], ["count"])

    def test_nested_braces_in_the_type(self):
        got = paramdrift.js_doc_params(" @param {Array<{x: number}>} points ")
        self.assertEqual([n for n, _ in got], ["points"])

    def test_optional_and_defaulted(self):
        got = paramdrift.js_doc_params(
            " @param {number} [retries] a\n @param {number} [delay=250] b ")
        self.assertEqual([n for n, _ in got], ["retries", "delay"])

    def test_a_path_names_a_field_not_an_argument(self):
        """`opts.retries` documents a field of `opts`. Only the root counts."""
        got = paramdrift.js_doc_params(
            " @param {Object} opts\n @param {number} opts.retries\n"
            " @param {number} [opts.delay=1] ")
        self.assertEqual([n for n, _ in got], ["opts"])

    def test_variadic(self):
        got = paramdrift.js_doc_params(" @param {...number} values ")
        self.assertEqual([n for n, _ in got], ["values"])

    def test_arg_and_argument_are_the_same_tag(self):
        got = paramdrift.js_doc_params(" @arg a x\n @argument b y ")
        self.assertEqual([n for n, _ in got], ["a", "b"])

    def test_a_tag_with_nothing_after_it_reads_nothing(self):
        """The scanner must not cross a newline looking for a name."""
        self.assertEqual(paramdrift.js_doc_params(" @param\n count how many "),
                         [])

    def test_a_dash_is_not_a_name(self):
        self.assertEqual(paramdrift.js_doc_params(" @param {string} - a name "),
                         [])

    def test_typedef_blocks_are_not_bound(self):
        self.assertIsNone(paramdrift.js_doc_params(
            " @callback Handler\n @param {string} message "))
        self.assertIsNone(paramdrift.js_doc_params(
            " @typedef Options\n @param {string} message "))

    def test_an_email_is_not_a_tag(self):
        self.assertEqual(paramdrift.js_doc_params(" write to me@param.io "), [])


class TestSignature(unittest.TestCase):
    def sig(self, window):
        return paramdrift.js_signature(window)

    def test_function_declaration(self):
        self.assertEqual(self.sig("function greet(a, b) {"), ("greet", ["a", "b"]))

    def test_exported_async(self):
        self.assertEqual(self.sig("export async function greet(a) {"),
                         ("greet", ["a"]))

    def test_arrow_assigned_to_a_const(self):
        self.assertEqual(self.sig("const greet = (a, b) => {"), ("greet", ["a", "b"]))

    def test_method_shorthand(self):
        self.assertEqual(self.sig("  greet(a) {"), ("greet", ["a"]))

    def test_typescript_types_and_return(self):
        self.assertEqual(
            self.sig("function greet(a: string, b?: number = 3): void {"),
            ("greet", ["a", "b"]))

    def test_comma_inside_a_generic_type_is_not_a_separator(self):
        self.assertEqual(
            self.sig("function greet(a: Map<string, number>, b: string) {"),
            ("greet", ["a", "b"]))

    def test_a_paren_inside_generics_is_not_the_parameter_list(self):
        self.assertEqual(
            self.sig("function run<T extends (x: number) => void>(cb: T) {"),
            ("run", ["cb"]))

    def test_constructor_properties_keep_their_names(self):
        self.assertEqual(
            self.sig("constructor(private readonly a: X, public b: Y) {"),
            ("constructor", ["a", "b"]))

    def test_this_is_not_an_argument(self):
        self.assertEqual(self.sig("function greet(this: Window, a) {"),
                         ("greet", ["a"]))

    def test_rest(self):
        self.assertEqual(self.sig("function greet(a, ...rest) {"),
                         ("greet", ["a", "rest"]))

    def test_default_containing_an_arrow(self):
        self.assertEqual(self.sig("function greet(cb = () => 1, b) {"),
                         ("greet", ["cb", "b"]))

    def test_default_containing_a_comma_in_a_string(self):
        self.assertEqual(self.sig("function greet(sep = ', ', b) {"),
                         ("greet", ["sep", "b"]))

    def test_destructured_argument_has_no_name(self):
        symbol, params = self.sig("function greet({a, b}) {")
        self.assertIn(paramdrift.OPAQUE, params)

    def test_keyword_paren_is_not_a_signature(self):
        self.assertIsNone(self.sig("if (ready) {"))

    def test_a_statement_between_breaks_the_binding(self):
        """A file header followed by imports must not bind to a function."""
        self.assertIsNone(self.sig(
            "\nimport fs from 'fs';\n\nfunction greet(a) {"))

    def test_a_class_body_is_not_a_parameter_list(self):
        self.assertIsNone(self.sig("\nexport const VERSION = 2;\n"))

    def test_empty_parameter_list(self):
        self.assertEqual(self.sig("function now() {"), ("now", []))


class TestFindings(unittest.TestCase):
    def test_the_species(self):
        src = ("/**\n * @param {string} oldName the thing\n */\n"
               "function greet(newName) {}\n")
        self.assertEqual(names(src), ["oldName"])

    def test_the_line_is_the_line_of_the_tag(self):
        src = ("\n\n/**\n * something\n * @param {string} oldName x\n */\n"
               "function greet(newName) {}\n")
        self.assertEqual(findings(src).findings[0]["line"], 5)

    def test_a_correct_comment_is_silent(self):
        src = ("/**\n * @param {string} name the thing\n */\n"
               "function greet(name) {}\n")
        self.assertEqual(names(src), [])

    def test_options_object_is_silent(self):
        src = ("/**\n * @param {Object} opts o\n * @param {number} opts.retries r\n"
               " */\nfunction run(opts) {}\n")
        self.assertEqual(names(src), [])

    def test_destructuring_is_skipped_not_guessed(self):
        src = ("/**\n * @param {Object} options o\n * @param {number} options.a a\n"
               " */\nfunction run({a, b}) {}\n")
        report = findings(src)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.opaque, 1)

    def test_callback_block_makes_no_finding(self):
        src = ("/**\n * @callback Handler\n * @param {string} message m\n */\n"
               "function attach(handler) {}\n")
        report = findings(src)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.blocks_typedef, 1)

    def test_a_documented_argument_the_function_does_not_take_at_all(self):
        src = ("/**\n * @param {string} name x\n */\nfunction now() {}\n")
        self.assertEqual(names(src), ["name"])

    def test_typescript_file(self):
        src = ("/**\n * @param cb the callback\n */\n"
               "export function run<T>(callback: T): void {}\n")
        self.assertEqual(names(src, "a.ts"), ["cb"])

    def test_a_comment_not_followed_by_a_declaration_is_counted_not_reported(self):
        src = ("/**\n * @param {string} name x\n */\n"
               "export const VERSION = 2;\n")
        report = findings(src)
        self.assertEqual(report.findings, [])
        self.assertEqual(report.blocks_with_params, 1)
        self.assertEqual(report.by_lang["js"], [0, 1])

    def test_coverage_counts_both_ways(self):
        src = ("/**\n * @param a x\n */\nfunction one(a) {}\n\n"
               "/**\n * @param b y\n */\nfunction two(b) {}\n")
        self.assertEqual(findings(src).by_lang["js"], [2, 2])


class TestCli(unittest.TestCase):
    def run_on(self, files):
        with tempfile.TemporaryDirectory() as d:
            for name, body in files.items():
                path = os.path.join(d, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(body)
            out = os.path.join(d, "out.json")
            p = subprocess.run(
                [sys.executable, os.path.join(HERE, "paramdrift.py"), d,
                 "--json", out],
                capture_output=True, text=True)
            with open(out, encoding="utf-8") as fh:
                return p, json.load(fh)

    def test_exit_code_follows_findings(self):
        p, found = self.run_on({
            "src/a.js": "/**\n * @param {string} old x\n */\nfunction f(fresh) {}\n"})
        self.assertEqual(p.returncode, 1)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["hard"])
        self.assertIn("=== Coverage ===", p.stdout)
        self.assertIn("% parsed)", p.stdout)

    def test_clean_tree_exits_zero(self):
        p, found = self.run_on({
            "src/a.js": "/**\n * @param {string} name x\n */\nfunction f(name) {}\n"})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(found, [])

    def test_minified_file_is_skipped(self):
        long_line = "var a=1;" * 900
        p, found = self.run_on({"dist.js": long_line})
        self.assertEqual(p.returncode, 0)
        self.assertIn("files skipped:          1", p.stdout)


if __name__ == "__main__":
    unittest.main()
