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

    # The four below are regressions. Each one was a wrong answer on a live
    # tree, not a case anybody thought of at a desk.

    def test_overloads_in_an_interface_are_unioned(self):
        """hono documents `HTMLRespond` once above two call signatures. Binding
        to the first alone reports the second one's argument as drift."""
        symbol, params = self.sig(
            "interface HTMLRespond {\n"
            "  <T extends string>(html: T, status?: number): Response\n"
            "  <T extends string>(html: T, init?: ResponseOrInit): Response\n"
            "}\n")
        self.assertEqual(params, ["html", "status", "init"])

    def test_overloads_of_a_function_are_unioned(self):
        symbol, params = self.sig(
            "export function on(a: string): void;\n"
            "export function on(a: string, b: number): void;\n"
            "export function on(a: any, b?: any) {\n")
        self.assertEqual(params, ["a", "b"])

    def test_a_one_argument_arrow_needs_no_parentheses(self):
        """Lighthouse writes `/** @param {Event} e */ e => {` inline. Walking
        past it binds the comment to the first call in the body."""
        self.assertEqual(self.sig(" e => {\n  const el = document.q('x');"),
                         ("", ["e"]))

    def test_a_call_statement_is_not_a_declaration(self):
        """A closing bracket followed by a semicolon has the shape of a
        TypeScript overload and the meaning of a call."""
        self.assertIsNone(self.sig("\ndocument.querySelector('div#lh-log');\n"))

    def test_a_body_ends_the_reading(self):
        """A function must not take the names of the functions inside it."""
        self.assertEqual(self.sig("function outer(a) {\n  inner(b, c);\n}"),
                         ("outer", ["a"]))


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


class TestJava(unittest.TestCase):
    """Javadoc. The markup is the same tag, the declaration is not."""

    def sig(self, window):
        return paramdrift.java_signature(window)

    def test_plain_method(self):
        self.assertEqual(self.sig("public void greet(String a, int b) {"),
                         ("greet", ["a", "b"]))

    def test_the_name_is_the_last_token(self):
        self.assertEqual(
            self.sig("public static <T> void build(final Map<String, List<T>> in, "
                     "int n) throws IOException {"),
            ("build", ["in", "n"]))

    def test_annotations_are_stepped_over(self):
        """`@SuppressWarnings({\"a\"})` holds braces that are not a body and a
        bracket that is not a parameter list."""
        self.assertEqual(
            self.sig('@SuppressWarnings({"a", "b"})\n  public void greet(String a) {'),
            ("greet", ["a"]))

    def test_parameter_annotations(self):
        self.assertEqual(self.sig("void greet(@Nullable String a, @Named(\"x\") int b) {"),
                         ("greet", ["a", "b"]))

    def test_a_declaration_with_no_body(self):
        """An interface method ends at the semicolon. In JavaScript that shape
        is a call statement, which is why the two languages part here."""
        self.assertEqual(self.sig("void greet(String a) throws IOException;"),
                         ("greet", ["a"]))

    def test_varargs_and_arrays(self):
        self.assertEqual(self.sig("void greet(int... rest) {"), ("greet", ["rest"]))
        self.assertEqual(self.sig("void greet(String[] a, String b[]) {"),
                         ("greet", ["a", "b"]))

    def test_a_record_header_is_a_parameter_list(self):
        self.assertEqual(self.sig("public record Point(int x, int y) {"),
                         ("Point", ["x", "y"]))

    def test_a_field_is_not_a_declaration_to_bind_to(self):
        self.assertIsNone(self.sig("private static final int LIMIT = 10;"))

    def test_a_type_parameter_tag_is_not_reported(self):
        """`@param <T>` documents a type parameter, and binding one needs the
        enclosing class as well as the method."""
        self.assertEqual(paramdrift.java_doc_params(" @param <T> the element type "), [])

    def test_the_species(self):
        src = ("/**\n * @param bean the thing\n */\n"
               "V getValue(Object instance);\n")
        self.assertEqual(names(src, "a.java"), ["bean"])

    def test_a_correct_comment_is_silent(self):
        src = ("/**\n * @param instance the thing\n */\n"
               "V getValue(Object instance);\n")
        self.assertEqual(names(src, "a.java"), [])

    def test_overloads_are_not_unioned_in_java(self):
        """Each Java overload carries its own comment, so unioning would only
        hide findings."""
        symbol, params = self.sig("void on(String a) {\n}\nvoid on(int b) {\n}")
        self.assertEqual(params, ["a"])


class TestCSharp(unittest.TestCase):
    """XML doc. The only dialect here whose markup is not a tag."""

    def sig(self, window):
        return paramdrift.cs_signature(window)

    def test_the_name_is_a_quoted_attribute(self):
        self.assertEqual(
            [n for n, _ in paramdrift.cs_doc_params(
                ' <param name="a">x</param>\n <typeparam name="T">y</typeparam>')],
            ["a"])

    def test_inheritdoc_is_not_bound(self):
        self.assertIsNone(paramdrift.cs_doc_params(' <inheritdoc/>\n <param name="a">x</param>'))

    def test_plain_method(self):
        self.assertEqual(self.sig("public void Greet(string a, int b = 3) {"),
                         ("Greet", ["a", "b"]))

    def test_type_parameters_come_after_the_name(self):
        """Java puts them before the return type, C# after the name, so the
        token in front of the bracket is the closing angle."""
        self.assertEqual(
            self.sig("public Task<T> Get<T>(this string s, out int n) where T : class {"),
            ("Get", ["s", "n"]))

    def test_attributes_and_directives_are_stepped_over(self):
        self.assertEqual(
            self.sig('#if FEATURE_X\n  [MessageTemplateFormatMethod("t")]\n'
                     '  void Write(string t);'),
            ("Write", ["t"]))

    def test_a_directive_between_the_bracket_and_the_body(self):
        """Serilog writes the body behind `#if`, and 78 of its 372 documented
        comments were unbound by that alone."""
        self.assertEqual(
            self.sig("ILogger ForContext(IEnumerable<T> enrichers)\n#if FEATURE_X\n{"),
            ("ForContext", ["enrichers"]))

    def test_a_constructor_chained_to_another_one(self):
        self.assertEqual(
            self.sig("public CodedOutputStream(Stream output) : this(output, 4096) { }"),
            ("CodedOutputStream", ["output"]))

    def test_an_operator_has_no_identifier_before_the_bracket(self):
        symbol, params = self.sig(
            "public static bool operator ==(ByteString lhs, ByteString rhs) {")
        self.assertEqual(params, ["lhs", "rhs"])

    def test_a_call_is_not_a_declaration(self):
        self.assertIsNone(self.sig("return Foo(x);"))
        self.assertIsNone(self.sig("var y = Foo(x);"))
        self.assertIsNone(self.sig("obj.Foo(x);"))

    def test_the_species(self):
        src = ('/// <param name="oldName">x</param>\n'
               'public void Greet(string newName) { }\n')
        self.assertEqual(names(src, "a.cs"), ["oldName"])


class TestPhp(unittest.TestCase):
    """PHPDoc. The sigil does the work, so the type never needs reading."""

    def sig(self, window):
        return paramdrift.php_signature(window)

    def test_the_name_is_the_dollar_token(self):
        got = paramdrift.php_doc_params(
            " @param array<string, list<int>> $rows the rows\n @param int $n\n")
        self.assertEqual([n for n, _ in got], ["rows", "n"])

    def test_a_generic_type_with_a_comma_needs_no_parsing(self):
        self.assertEqual(self.sig("public function greet(string $a, int $b = 3): void {"),
                         ("greet", ["a", "b"]))

    def test_variadic_and_by_reference(self):
        self.assertEqual(self.sig("function greet(array &$rows, ...$rest) {"),
                         ("greet", ["rows", "rest"]))

    def test_promoted_constructor_properties(self):
        self.assertEqual(self.sig("public function __construct(private string $a) {"),
                         ("__construct", ["a"]))

    def test_an_interface_method_ends_at_the_semicolon(self):
        self.assertEqual(self.sig("public function greet(string $a);"), ("greet", ["a"]))

    def test_a_call_is_not_a_declaration(self):
        """WP-CLI documents a hook's arguments in a docblock above the call
        that fires it, and a bracket followed by a semicolon has the same
        shape as an abstract method."""
        self.assertIsNone(self.sig("return WP_CLI::do_hook('x', $all_formats);"))

    def test_func_get_args_means_nothing_to_compare(self):
        symbol, params = self.sig(
            "public static function decodeJson()\n{\n    $args = func_get_args();")
        self.assertEqual(params, [paramdrift.OPAQUE])

    def test_the_species(self):
        src = ("/**\n * @param string $oldName x\n */\n"
               "function greet(string $newName) {}\n")
        self.assertEqual(names(src, "a.php"), ["oldName"])


class TestRuby(unittest.TestCase):
    """YARD. Both orders of name and type are written in real code."""

    def sig(self, window):
        return paramdrift.rb_signature(window)

    def test_the_name_is_the_token_not_in_brackets(self):
        got = paramdrift.rb_doc_params(
            " @param name [String] the name\n @param [Integer] count how many\n")
        self.assertEqual([n for n, _ in got], ["name", "count"])

    def test_every_kind_of_parameter(self):
        self.assertEqual(self.sig("def greet(a, b = 1, *rest, key:, **opts, &blk)"),
                         ("greet", ["a", "b", "rest", "key", "opts", "blk"]))

    def test_parentheses_are_optional(self):
        self.assertEqual(self.sig("def self.greet a, b"), ("greet", ["a", "b"]))

    def test_a_visibility_modifier_in_front(self):
        self.assertEqual(self.sig("private def greet(a)"), ("greet", ["a"]))

    def test_something_that_is_not_a_def(self):
        self.assertIsNone(self.sig("attr_accessor :name"))

    def test_the_species(self):
        src = ("# @param [Range] range or node\n"
               "# @param [Integer] size\n"
               "def remove_preceding(node_or_range, size)\n")
        self.assertEqual(names(src, "a.rb"), ["range"])


class TestRust(unittest.TestCase):
    """rustdoc has no tag at all, only a markdown section."""

    def sig(self, window):
        return paramdrift.rs_signature(window)

    def test_only_backticked_names_under_an_arguments_heading(self):
        got = paramdrift.rs_doc_params(
            "   Does a thing with `x`.\n\n   # Arguments\n\n"
            "   * `a` - the first\n   * `b` - the second\n\n"
            "   # Panics\n\n   * `c` - not an argument\n")
        self.assertEqual([n for n, _ in got], ["a", "b"])

    def test_no_arguments_section_means_nothing_documented(self):
        self.assertEqual(paramdrift.rs_doc_params("   Takes `a` and `b`.\n"), [])

    def test_the_name_is_before_the_colon(self):
        """The opposite end from Java, where the name is the last token."""
        self.assertEqual(self.sig("pub fn greet(a: &str, b: usize) -> Result<()> {"),
                         ("greet", ["a", "b"]))

    def test_the_receiver_is_not_an_argument(self):
        self.assertEqual(self.sig("pub fn greet(&mut self, a: u8) -> u8 {"),
                         ("greet", ["a"]))

    def test_an_apostrophe_is_a_lifetime_and_not_a_quote(self):
        """🔴 A blanker told to treat `'` as a string opener erased everything
        between two lifetimes. On qdrant that turned a 7 argument signature
        into a 1 argument one and manufactured 6 findings."""
        self.assertEqual(
            self.sig("fn find<'a>(data: impl Iterator<Item = u8> + 'a + Clone, "
                     "rows: &[u8], count: usize) -> u8 {"),
            ("find", ["data", "rows", "count"]))

    def test_attributes_and_where_clauses(self):
        self.assertEqual(
            self.sig("#[inline]\npub fn greet<T: Trait>(a: T, b: u8) -> u8 where T: Copy {"),
            ("greet", ["a", "b"]))

    def test_the_species(self):
        src = ("/// Does a thing.\n///\n/// # Arguments\n///\n"
               "/// * `op` - the operation\n"
               "pub fn handle(op_num: u64, operation: F) -> bool {\n")
        self.assertEqual(names(src, "a.rs"), ["op"])
