#!/usr/bin/env python3
"""Tests for swiftdrift.

Every case below was paid for with a false finding on a real Apple tree, and
the comment above each one says which. That is the point of the file: it is a
record of what Swift does that a naive parser does not expect.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swiftdrift as sd


def params_of(src: str):
    """(label, name) pairs of the first declaration in a snippet."""
    code, _docs = sd.scan(src)
    decls = sd.declarations(code)
    assert decls, "no declaration found"
    return decls[0].params


def names_of_doc(src: str):
    _code, docs = sd.scan(src)
    assert docs, "no doc comment found"
    return sd.documented_names(docs[0].text)


class Signature(unittest.TestCase):
    def test_an_argument_has_two_names(self):
        """`from start:` is one argument with a label and a name."""
        self.assertEqual(params_of("func move(from start: Point) {}"),
                         [("from", "start")])

    def test_a_suppressed_label(self):
        self.assertEqual(params_of("func greet(_ person: String) {}"),
                         [(None, "person")])

    def test_label_and_name_the_same(self):
        self.assertEqual(params_of("func greet(person: String) {}"),
                         [("person", "person")])

    def test_an_arrow_is_not_a_closing_angle_bracket(self):
        """swift-algorithms, Keyed.swift: `->` inside a closure type drove the
        depth counter negative, and the commas of `(Key, Element, Element)`
        split one argument into three."""
        src = ("func keyed<Key>(\n"
               "  by keyForValue: (Element) throws -> Key,\n"
               "  resolvingConflictsWith resolve: (Key, Element, Element) throws -> Element\n"
               ") rethrows -> [Key: Element] {}")
        self.assertEqual(params_of(src),
                         [("by", "keyForValue"), ("resolvingConflictsWith", "resolve")])

    def test_nested_generics_in_the_clause(self):
        """swift-composable-architecture, Store.swift: a non-greedy match up to
        the first `>` stopped inside `init<R: Reducer<State, Action>>`."""
        src = ("init<R: Reducer<State, Action>>(\n"
               "  initialState: @autoclosure () -> R.State,\n"
               "  reducer: () -> R\n"
               ") {}")
        self.assertEqual(params_of(src),
                         [("initialState", "initialState"), ("reducer", "reducer")])

    def test_a_result_builder_carries_its_own_generics(self):
        """swift-composable-architecture, CombineReducers.swift: stripping
        `@ReducerBuilder` but not its `<State, Action>` left the generics
        sitting where the name should be."""
        src = "init(@ReducerBuilder<State, Action> _ build: () -> Reducers) {}"
        self.assertEqual(params_of(src), [(None, "build")])

    def test_a_dictionary_type_does_not_split_the_list(self):
        src = "func f(a: [String: Int], b: Int) {}"
        self.assertEqual(params_of(src), [("a", "a"), ("b", "b")])

    def test_a_default_value_with_commas(self):
        src = "func f(xs: [Int] = [1, 2, 3], flag: Bool = false) {}"
        self.assertEqual(params_of(src), [("xs", "xs"), ("flag", "flag")])

    def test_a_shift_operator_in_a_default_value(self):
        """swift-nio, NIOWebSocketClientUpgrader.swift: `= 1 << 14` opened an
        angle level that never closed, so the list stopped splitting and two
        of the four arguments went missing, both reported as findings."""
        src = ("public init(\n"
               "  requestKey: String = randomRequestKey(),\n"
               "  maxFrameSize: Int = 1 << 14,\n"
               "  automaticErrorHandling: Bool = true,\n"
               "  upgradePipelineHandler: @escaping (Channel) -> Void\n"
               ") {}")
        self.assertEqual([n for _l, n in params_of(src)],
                         ["requestKey", "maxFrameSize",
                          "automaticErrorHandling", "upgradePipelineHandler"])

    def test_two_closing_angles_are_not_a_shift(self):
        """The other side of the same coin: `Reducer<State, Action>>` ends a
        nested clause, and reading it as a shift left the clause open."""
        src = ("init<R: Reducer<State, Action>>(initialState: State, reducer: R) {}")
        self.assertEqual([n for _l, n in params_of(src)],
                         ["initialState", "reducer"])

    def test_a_failable_init(self):
        self.assertEqual(params_of("init?(raw value: Int) {}"), [("raw", "value")])

    def test_an_operator(self):
        src = "static func == (lhs: X, rhs: X) -> Bool {}"
        self.assertEqual(params_of(src), [("lhs", "lhs"), ("rhs", "rhs")])

    def test_a_subscript(self):
        self.assertEqual(params_of("subscript(position: Index) -> Element {}"),
                         [("position", "position")])

    def test_no_arguments(self):
        self.assertEqual(params_of("func reset() {}"), [])


class Lexing(unittest.TestCase):
    def test_block_comments_nest(self):
        """Legal Swift, illegal C. A C-shaped stripper ends the comment early
        and reads the prose after it as code."""
        src = ('/* outer /* inner */ func notReal(x: Int) {} */\n'
               'func real(y: Int) {}')
        code, _ = sd.scan(src)
        decls = sd.declarations(code)
        self.assertEqual(len(decls), 1)
        self.assertEqual(decls[0].params, [("y", "y")])

    def test_string_interpolation_holds_code(self):
        src = ('let s = "\\(items.map { f(x: 1) })"\n'
               'func real(y: Int) {}')
        code, _ = sd.scan(src)
        self.assertEqual(len(sd.declarations(code)), 1)

    def test_a_raw_string_keeps_its_backslashes(self):
        src = ('let r = #"a \\(not) interpolation "quote" "#\n'
               'func real(y: Int) {}')
        code, _ = sd.scan(src)
        self.assertEqual(len(sd.declarations(code)), 1)

    def test_a_multiline_string(self):
        src = ('let m = """\nfunc fake(z: Int) {}\n"""\n'
               'func real(y: Int) {}')
        code, _ = sd.scan(src)
        self.assertEqual(len(sd.declarations(code)), 1)

    def test_four_slashes_are_not_a_doc_comment(self):
        _code, docs = sd.scan("//// - Parameter x: no\nfunc f(y: Int) {}")
        self.assertEqual(docs, [])

    def test_line_numbers_survive_blanking(self):
        src = "// filler\n/* two\nlines */\nfunc f(y: Int) {}"
        code, _ = sd.scan(src)
        self.assertEqual(sd.declarations(code)[0].line, 4)


class DocComment(unittest.TestCase):
    def test_single_parameter_form(self):
        self.assertEqual(names_of_doc("/// - Parameter person: who\nfunc f() {}"),
                         ["person"])

    def test_the_list_form(self):
        src = ("/// - Parameters:\n"
               "///   - first: one\n"
               "///   - second: two\n"
               "func f() {}")
        self.assertEqual(names_of_doc(src), ["first", "second"])

    def test_a_deeper_level_documents_a_closure_not_the_function(self):
        """swift-composable-architecture, OnChange.swift: `oldValue` and
        `state` are the arguments of the closure `perform` takes, and reading
        them as arguments of `onChange` produced two false findings."""
        src = ("/// - Parameters:\n"
               "///   - toValue: a closure returning a value\n"
               "///   - perform: a closure to run\n"
               "///     - `oldValue`: the old value\n"
               "///     - `state`: the current state\n"
               "func f() {}")
        self.assertEqual(names_of_doc(src), ["toValue", "perform"])

    def test_a_shallower_item_ends_the_list(self):
        """swift-nio, Codec.swift: a malformed `- return:` sits at the outer
        level right under `- Parameters:`. Taking the smallest indent as the
        base read `return` as an argument and dropped the real one."""
        src = ("/// - Parameters:\n"
               "///   - buffer: the buffer to check\n"
               "/// - return: true if memory should be reclaimed\n"
               "func f() {}")
        self.assertEqual(names_of_doc(src), ["buffer"])

    def test_a_returns_callout_ends_the_list(self):
        src = ("/// - Parameters:\n"
               "///   - buffer: the buffer\n"
               "/// - Returns: a value\n"
               "func f() {}")
        self.assertEqual(names_of_doc(src), ["buffer"])

    def test_a_block_doc_comment(self):
        src = ("/**\n * - Parameter person: who\n */\nfunc f() {}")
        self.assertEqual(names_of_doc(src), ["person"])


class Matching(unittest.TestCase):
    def _decl(self, src):
        code, _ = sd.scan(src)
        return sd.declarations(code)[0]

    def test_the_parameter_name_is_accepted(self):
        d = self._decl("func move(from start: Point) {}")
        self.assertTrue(d.accepts("start"))

    def test_the_label_is_accepted_too(self):
        """Apple documents the parameter name, plenty of code documents the
        label. Insisting on one of them would manufacture findings."""
        d = self._decl("func move(from start: Point) {}")
        self.assertTrue(d.accepts("from"))

    def test_a_name_that_is_neither_is_a_finding(self):
        d = self._decl("func move(from start: Point) {}")
        self.assertFalse(d.accepts("origin"))

    def test_backticks_are_stripped_on_both_sides(self):
        d = self._decl("func f(`default` value: Int) {}")
        self.assertTrue(d.accepts("default"))
        self.assertTrue(d.accepts("value"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
