#!/usr/bin/env python3
"""Tests for docdrift.py: docstrings against signatures. Run: python3 test_docdrift.py"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import docdrift


def findings_in(code):
    """A run over one file without building a tree on disk."""
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "module.py"
    p.write_text(code, encoding="utf-8")
    try:
        return docdrift.scan_file(p, "sample")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def hard_in(code):
    return [h for h in findings_in(code) if docdrift.is_hard(h)]


def soft_in(code):
    return [h for h in findings_in(code) if not docdrift.is_hard(h)]


# ------------------------------------------------------------ class A: renames


class TestClassA(unittest.TestCase):
    def test_documented_name_missing_from_signature(self):
        h = hard_in('''
def f(model, n):
    """Does the thing.

    Parameters
    ----------
    MODEL : str
        The model.
    n : int
        How many.
    """
''')
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["param"], "MODEL")
        self.assertEqual(h[0]["func"], "f")

    def test_matching_names_stay_quiet(self):
        self.assertEqual(hard_in('''
def f(model, n):
    """Does the thing.

    Parameters
    ----------
    model : str
        The model.
    n : int
        How many.
    """
'''), [])

    def test_kwargs_lifts_the_claim(self):
        """Anything may legitimately be documented for a function with **kwargs."""
        self.assertEqual(hard_in('''
def f(a, **kwargs):
    """Does the thing.

    Parameters
    ----------
    a : int
        First.
    whatever : str
        Second.
    """
'''), [])

    def test_self_and_cls_are_no_loss(self):
        self.assertEqual(hard_in('''
class K:
    def m(self, a):
        """Does the thing.

        Parameters
        ----------
        self : K
            Itself.
        a : int
            First.
        """
'''), [])

    def test_a_property_is_not_judged(self):
        """In networkx `G.edges` is a cached_property returning a callable view.
        The names nbunch and data in the docstring belong to that view rather than
        to the function. Of 44 hard findings on networkx, 41 were this.
        """
        for dec in ("property", "cached_property", "functools.cached_property"):
            self.assertEqual(hard_in(f'''
import functools
class G:
    @{dec}
    def edges(self):
        """The edges.

        Parameters
        ----------
        nbunch : list
            The nodes.
        data : bool
            The data.
        """
'''), [], dec)

    def test_a_compatibility_decorator_adds_names(self):
        """statsmodels: `@deprecate_kwarg("random_state", "rng")`.

        The function still accepts the old name, the docstring honestly documents
        both, and the signature knows nothing about the old one. Reading the
        signature alone, the scanner declared a live name nonexistent: thirty false
        findings out of
        fifty-six. The case was brought in from a live run.
        """
        self.assertEqual(hard_in('''
@deprecate_kwarg("random_state", "rng")
def rvs(self, size, rng=None):
    """Does the thing.

    Parameters
    ----------
    random_state : int
        The old name.
    rng : int
        The new name.
    """
'''), [])

    def test_a_compatibility_decorator_with_a_dict(self):
        self.assertEqual(hard_in('''
@renamed_kwargs({"old_name": "new_name"})
def f(new_name=None):
    """Does the thing.

    Parameters
    ----------
    old_name : int
        First.
    """
'''), [])

    def test_a_compatibility_decorator_with_keywords(self):
        self.assertEqual(hard_in('''
@deprecated_alias(old_name="new_name")
def f(new_name=None):
    """Does the thing.

    Parameters
    ----------
    old_name : int
        First.
    """
'''), [])

    def test_an_opaque_decorator_dismisses_class_a(self):
        """A compatibility decorator is there and the names it accepts are not.

        Claiming "there is no such name" while knowing the source incompletely is
        not allowed. Same species as a CI matrix hidden behind
        `${{ env.MIN_PYTHON }}`.
        """
        self.assertEqual(hard_in('''
@deprecated
def f(new_name=None):
    """Does the thing.

    Parameters
    ----------
    old_name : int
        First.
    """
'''), [])

    def test_a_compatibility_decorator_still_reports_other_names(self):
        """Only the names the decorator actually spells out get dismissed."""
        h = hard_in('''
@deprecate_kwarg("random_state", "rng")
def rvs(self, size, rng=None):
    """Does the thing.

    Parameters
    ----------
    random_state : int
        The old name.
    nonexistent : int
        Simply invented.
    """
''')
        self.assertEqual([x["param"] for x in h], ["nonexistent"])

    def test_a_todo_note_is_no_argument(self):
        """statsmodels `gradient_momcond`: the line `TODO: looks like not used
        yet` inside a Parameters section is shaped exactly like `name : type`."""
        self.assertEqual(hard_in('''
def f(params):
    """Does the thing.

    Parameters
    ----------
    params : ndarray
        First.

    TODO: looks like not used yet
    FIXME: this one too
    """
'''), [])

    def test_an_ordinary_decorator_keeps_the_check(self):
        """Skipping everything decorated would be far too broad."""
        h = hard_in('''
def deco(f):
    return f

@deco
def f(model):
    """Does the thing.

    Parameters
    ----------
    MODEL : str
        The model.
    """
''')
        self.assertEqual(len(h), 1)

    def test_a_section_inside_an_example_does_not_count(self):
        """In mne a doctest prints someone else's docstring in full, and its
        Parameters section comes out unindented, indistinguishable from a real one.
        That is how `copy_function_doc_to_method_doc` gained arguments `a` and `b`.
        """
        self.assertEqual(hard_in('''
def f(source):
    """Does the thing.

    Parameters
    ----------
    source : function
        Where from.

    Examples
    --------
    >>> print(other.__doc__)
    Docstring.
    <BLANKLINE>
    Parameters
    ----------
    a : int
        First.
    b : int
        Second.
    """
'''), [])

    def test_other_sections_before_examples_are_read(self):
        """`Other Parameters` is a legitimate section and stands before Examples."""
        h = hard_in('''
def f(a):
    """Does the thing.

    Parameters
    ----------
    a : int
        First.

    Other Parameters
    ----------------
    nonexistent : int
        Second.
    """
''')
        self.assertEqual([x["param"] for x in h], ["nonexistent"])

    def test_google_style_is_read(self):
        """Was a stated blind spot until 19.08.2026.

        A run on PySceneDetect read 51 files, reported `functions with
        Parameters: 0` and found nothing, because the whole repository writes
        `Args:`. The project came back looking clean while nothing had been
        compared.
        """
        h = findings_in('''
def f(model):
    """Does the thing.

    Args:
        MODEL: the model
    """
''')
        self.assertEqual([x["param"] for x in h], ["MODEL"])

    def test_a_google_description_is_not_mistaken_for_a_type(self):
        """The colon means different things in the two dialects.

        numpydoc puts the type after the colon, Google style puts the
        description there. Reading a description as a type would let
        `Defaults to 5` be pulled out of any sentence containing the words.
        """
        h = findings_in('''
def f(count=3):
    """Does the thing.

    Args:
        count: how many. Defaults to 5.
    """
''')
        self.assertEqual([(x["kind"], x.get("param")) for x in h], [("B", "count")])

    def test_a_wrapped_google_description_is_not_a_parameter(self):
        """A continuation line carrying a colon is the classic false positive.

        Same family as the wrapped URL that once became a parameter called
        `https` in the numpydoc parser.
        """
        self.assertEqual(findings_in('''
def f(path):
    """Does the thing.

    Args:
        path: where to write. See also:
            https://example.com/docs for the format.
    """
'''), [])

    def test_a_google_type_in_parentheses_is_read(self):
        h = findings_in('''
def f(name):
    """Does the thing.

    Args:
        nmae (str, optional): the name
    """
''')
        self.assertEqual([x["param"] for x in h], ["nmae"])

    def test_a_google_section_stops_at_the_next_one(self):
        """`Returns:` ends the arguments. Without that the return description
        parses as one more argument."""
        self.assertEqual(findings_in('''
def f(a):
    """Does the thing.

    Args:
        a: the first one

    Returns:
        result: what came out
    """
'''), [])

    def test_no_docstring(self):
        self.assertEqual(findings_in("def f(a):\n    return a\n"), [])

    def test_a_broken_file_does_not_crash(self):
        self.assertEqual(findings_in("def f(:\n"), [])


# ------------------------------------------------------ class B: default values


class TestClassB(unittest.TestCase):
    def test_default_mismatch(self):
        m = soft_in('''
def f(alpha=0.01):
    """Does the thing.

    Parameters
    ----------
    alpha : float, default 0
        The step.
    """
''')
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["real_default"], "0.01")

    def test_a_matching_default_stays_quiet(self):
        self.assertEqual(soft_in('''
def f(alpha=0.01):
    """Does the thing.

    Parameters
    ----------
    alpha : float, default 0.01
        The step.
    """
'''), [])

    def test_one_value_written_differently(self):
        """`True` and `true`, and quotes around a string, are the same thing."""
        self.assertEqual(soft_in('''
def f(flag=True, name="x"):
    """Does the thing.

    Parameters
    ----------
    flag : bool, default true
        First.
    name : str, default 'x'
        Second.
    """
'''), [])

    def test_a_sentinel_none_stays_quiet(self):
        """In networkx the code has None and the docstring says what it becomes.

        "create_using : graph type, default nx.Graph" with `create_using=None` is
        an idiom of the whole language. On networkx there were 80 such findings
        out of 168.
        """
        self.assertEqual(soft_in('''
def f(create_using=None):
    """Does the thing.

    Parameters
    ----------
    create_using : graph type, default nx.Graph
        The type.
    """
'''), [])

    def test_prose_instead_of_a_value_stays_quiet(self):
        for text in ("all nodes in G", "len(G", "first node in list(G"):
            self.assertEqual(soft_in(f'''
def f(n=1):
    """Does the thing.

    Parameters
    ----------
    n : int, default {text}
        First.
    """
'''), [], text)

    def test_one_number_in_two_notations(self):
        """`1e-8` against `1e-08` and octal `0o775` against `509`."""
        for doc, code in (("1e-8", "1e-08"), ("1.0e-6", "1e-06"), ("0o775", "0o775")):
            self.assertEqual(soft_in(f'''
def f(x={code}):
    """Does the thing.

    Parameters
    ----------
    x : number, default {doc}
        First.
    """
'''), [], doc)

    def test_a_boolean_does_not_equal_one(self):
        """True and 1 are different defaults and numeric comparison must not
        confuse them."""
        self.assertEqual(len(soft_in('''
def f(flag=1):
    """Does the thing.

    Parameters
    ----------
    flag : bool, default True
        First.
    """
''')), 1)

    def test_a_decimal_default_is_not_truncated(self):
        """`default 0.01` read as `0`: the dot sat in the list of stop characters
        and every decimal default became a mismatch."""
        self.assertEqual(soft_in('''
def f(alpha=0.01):
    """Does the thing.

    Parameters
    ----------
    alpha : float, default 0.01
        The step.
    """
'''), [])

    def test_a_computed_default_is_skipped(self):
        """`ast.literal_eval` stays quiet on an expression, which is correct."""
        self.assertEqual(soft_in('''
HUNDRED = 100

def f(n=HUNDRED):
    """Does the thing.

    Parameters
    ----------
    n : int, default 1
        First.
    """
'''), [])

    def test_a_default_finding_is_soft(self):
        """Free text is compared against a literal, so a human decides."""
        h = findings_in('''
def f(alpha=0.01):
    """Does the thing.

    Parameters
    ----------
    alpha : float, default 0
        The step.
    """
''')
        self.assertTrue(h)
        self.assertFalse(any(docdrift.is_hard(x) for x in h))


# ------------------------------------------------- coverage, not false findings


class TestCoverage(unittest.TestCase):
    """Two bugs where the tool looked at less than it reported.

    Both were brought in from outside. Neither produced a false finding, which
    is what makes them expensive: a report that quietly covers less looks
    cleaner, and cleaner reads as better.
    """

    def test_section_indent_comes_from_the_heading(self):
        """The base indent used to come from the first line that matched.

        A description wrapped onto the next line carries its own colon, and a
        wrapped URL carries one inside `https://`. That line then set the base
        indent, so `https` became a parameter and every real parameter of the
        function became invisible. Across the pool: 2654 functions never looked
        at, ibis 632 of 682, anndata 36 of 39, great-tables 119 of 132.
        """
        doc = """Get cube normalized with statistics cube.

    Parameters
    ----------
    cube:
        Input cube that will be normalized.
    statistics_cube:
        Cube that is used to normalize the input cube. Needs to be
        broadcastable to the input cube shape (see also
        https://scitools-iris.readthedocs.io/en/latest/userguide/cube_maths.
        html#calculating-a-cube-anomaly).
    normalize:
        Normalization operation.
    """
        names = [n for group, _t, _l in docdrift.doc_params(doc) for n in group]
        self.assertEqual(names, ["cube", "statistics_cube", "normalize"])
        self.assertNotIn("https", names)

    def test_a_name_without_a_type_is_a_parameter(self):
        """Modern numpydoc for annotated code writes no type: it is in the
        annotation. Requiring one made the whole function invisible."""
        h = hard_in('''
def f(model, n):
    """Does the thing.

    Parameters
    ----------
    MODEL:
        The model.
    n:
        How many.
    """
''')
        self.assertEqual([x["param"] for x in h], ["MODEL"])

    def test_a_name_without_a_colon_is_a_parameter(self):
        """ibis writes names with no colon at all, description indented under."""
        doc = """Build a case.

        Parameters
        ----------
        case_expr
            Predicate expression to use for this case.
        result_expr
            Value when the case predicate evaluates to true.
        """
        names = [n for group, _t, _l in docdrift.doc_params(doc) for n in group]
        self.assertEqual(names, ["case_expr", "result_expr"])

    def test_a_lone_word_with_no_description_is_no_parameter(self):
        """The guard against the previous rule going too wide."""
        doc = """Summary.

    Parameters
    ----------
    x : int
        Number.

    Notes
    -----
    Something
    """
        names = [n for group, _t, _l in docdrift.doc_params(doc) for n in group]
        self.assertEqual(names, ["x"])

    def test_a_file_that_fails_to_parse_is_counted_apart(self):
        """`files read` used to include files where `ast.parse` had raised.

        Three files of ESMValCore use syntax newer than the interpreter of the
        run; twelve functions vanished and the coverage line did not move.
        """
        d = tempfile.mkdtemp()
        try:
            # The live case was syntax newer than the interpreter: three files
            # of ESMValCore on 3.12 with the run on 3.9. Here the syntax is
            # broken for every interpreter, so the test does not depend on which
            # one runs it.
            pathlib.Path(d, "newer.py").write_text(
                "def f(:\n    return 1\n", encoding="utf-8")
            pathlib.Path(d, "fine.py").write_text("def g(a):\n    return a\n", encoding="utf-8")
            docdrift.scan(d)
            self.assertEqual(docdrift.COUNTS["unparsable"], 1)
            self.assertEqual(docdrift.COUNTS["files"] - docdrift.COUNTS["unparsable"], 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_short_underline_does_not_turn_a_section_into_a_parameter(self):
        """great-tables writes `Examples` under six dashes instead of eight.

        The reference parser requires the underline to match the header and,
        failing that, folds the section into Parameters: it reported
        `Examples`, `Returns`, `Raises` and `ValueError` as documented names.
        Our own rule takes three dashes or more, so it sees the section.
        Six of nine findings on that project were this.
        """
        d = tempfile.mkdtemp()
        pathlib.Path(d, "m.py").write_text('''
def md(text):
    """Interpret input as Markdown.

    Parameters
    ----------
    text
        The text.

    Examples
    ------
    See the docs.
    """
''')
        try:
            ours = [h["param"] for h in docdrift.scan(d)]
            self.assertEqual(ours, [], "a section header was taken for a parameter")
        finally:
            shutil.rmtree(d, ignore_errors=True)


    def test_a_section_heading_with_a_trailing_colon(self):
        """pyGSTi writes `Returns:` above the underline.

        Without allowing that colon the heading failed to match, the word fell
        through to the parameter rule and `Returns` was reported as a documented
        name. Found by running the reference parser beside our own: it was in
        the "ours only" column, which is where our own mistakes surface.
        """
        doc = """Apply the error generator.

    Parameters
    ----------
    errorgen : Label
        A label.

    Returns:
    --------
    A tuple.
    """
        names = [n for group, _t, _l in docdrift.doc_params(doc) for n in group]
        self.assertEqual(names, ["errorgen"])

    def test_an_older_interpreter_is_named_before_the_run(self):
        """anndata asks for 3.12; on 3.9 twenty-one files never parse."""
        d = tempfile.mkdtemp()
        try:
            pathlib.Path(d, "pyproject.toml").write_text(
                '[project]\nrequires-python = ">=3.99"\n', encoding="utf-8")
            self.assertIn("3.99", docdrift.interpreter_gap(d))
            pathlib.Path(d, "pyproject.toml").write_text(
                '[project]\nrequires-python = ">=3.0"\n', encoding="utf-8")
            self.assertEqual(docdrift.interpreter_gap(d), "")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ------------------------------------- the second engine: the reference parser


class TestNumpydocEngine(unittest.TestCase):
    """numpydoc parses the docstring instead of the rules written here.

    It is the reference implementation of the format, so it is the natural
    thing to check our own parser against. What it is NOT used for is
    `numpydoc.validate`: that imports the module it inspects, and running
    imports across foreign clones means executing their code.
    """

    def setUp(self):
        if not docdrift.numpydoc_available():
            self.skipTest("numpydoc is not installed")
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        pathlib.Path(self.dir, "m.py").write_text(text, encoding="utf-8")

    def test_the_reference_parser_finds_the_same_species(self):
        self.write('''
def f(model, n):
    """Does the thing.

    Parameters
    ----------
    MODEL : str
        The model.
    n : int
        How many.
    """
''')
        hits = docdrift.scan_numpydoc(self.dir)
        self.assertEqual([h["param"] for h in hits], ["MODEL"])

    def test_a_name_without_a_type_is_read(self):
        """The case that made 2654 functions invisible to our own parser."""
        self.write('''
def f(cube, normalize):
    """Does the thing.

    Parameters
    ----------
    cube:
        Input cube.
    missing:
        Not an argument.
    """
''')
        hits = docdrift.scan_numpydoc(self.dir)
        self.assertEqual([h["param"] for h in hits], ["missing"])

    def test_a_name_with_no_space_before_the_colon(self):
        """`create_using: NetworkX graph container, optional` in networkx.

        With no space before the colon numpydoc puts the whole line into the
        name and leaves the type empty. Splitting that on commas turned the
        word `optional` into a parameter, 291 times across the pool: an
        artefact of the adapter rather than of either parser. The name is cut
        at the first colon before anything else happens to it.
        """
        got = docdrift.doc_params_numpydoc("""Parse.

Parameters
----------
create_using: NetworkX graph container, optional
   Use given graph.
x, y : int
   A pair.
""")
        self.assertEqual([names for names, _t, _l in got], [["create_using"], ["x", "y"]])

    def test_both_engines_emit_the_same_shape(self):
        """The refuter and the sweep must not be able to tell them apart."""
        self.write('''
def f(model):
    """Does the thing.

    Parameters
    ----------
    MODEL : str
        The model.
    """
''')
        for h in docdrift.scan_numpydoc(self.dir) + docdrift.scan(self.dir):
            for key in ("kind", "file", "line", "func", "param", "sig"):
                self.assertIn(key, h)

# ---------------------------------------------------------------- kit contract


class TestContract(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, code):
        p = pathlib.Path(self.dir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")

    DRIFT = '''
def f(model):
    """Does the thing.

    Parameters
    ----------
    MODEL : str
        The model.
    """
'''

    def test_json_carries_hard_and_exit_code(self):
        self.write("package/module.py", self.DRIFT)
        out = os.path.join(self.dir, "out.json")
        code = docdrift.main([self.dir, "--json", out])
        self.assertEqual(code, 1)
        data = json.load(open(out, encoding="utf-8"))
        self.assertTrue(any(x["hard"] for x in data))

    def test_clean_means_zero(self):
        self.write("package/module.py", "def f(a):\n    return a\n")
        self.assertEqual(docdrift.main([self.dir]), 0)

    def test_test_and_example_directories_are_skipped(self):
        for rel in ("tests/module.py", "docs/module.py", "examples/module.py",
                    "package/test_module.py", "package/module_test.py",
                    "package/conftest.py"):
            self.write(rel, self.DRIFT)
        self.assertEqual(docdrift.scan(self.dir), [])

    def test_shared_skip_list_is_inherited(self):
        """A private skip list once drifted apart across three tools."""
        import common
        self.assertTrue(common.SKIP_DIRS <= docdrift.SKIP_DIRS)


class TestAuthorSwitchedItOff(unittest.TestCase):
    """A directive in the source saying this check does not apply here.

    statsmodels writes `# numpydoc ignore=PR01,PR02` on `apply_where`, where
    PR02 is "unknown parameters" word for word. The documented `xp` is
    deliberate and the author said so in the code; reporting it means telling a
    maintainer something they have already decided.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, text):
        p = pathlib.Path(self.dir, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    BODY = '''"""
    Run one of two functions.

    Parameters
    ----------
    cond : array
        The condition.
    xp : array_namespace, optional
        The standard-compatible namespace.
    """
    return cond
'''

    def test_the_directive_dismisses_the_finding(self):
        self.write("pkg/m.py",
                   "def apply_where(  # type: ignore[explicit-any] # numpydoc ignore=PR01,PR02\n"
                   "    cond, /, *, fill_value=None\n"
                   "):\n    " + self.BODY)
        self.assertEqual(docdrift.scan(self.dir), [])
        self.assertEqual(docdrift.COUNTS["suppressed"], 1)

    def test_a_directive_about_another_check_leaves_this_one_judging(self):
        """`EX01` is about missing examples and says nothing about parameters.

        Reading any directive as "stay quiet about everything" is how a rule
        stops being exact and starts hiding real findings.
        """
        self.write("pkg/m.py",
                   "def apply_where(  # numpydoc ignore=EX01,SA01\n"
                   "    cond, /, *, fill_value=None\n"
                   "):\n    " + self.BODY)
        self.assertEqual([h["param"] for h in docdrift.scan(self.dir)], ["xp"])

    def test_a_directive_with_no_codes_covers_everything(self):
        self.write("pkg/m.py",
                   "def apply_where(cond):  # numpydoc ignore\n    " + self.BODY)
        self.assertEqual(docdrift.scan(self.dir), [])

    def test_a_directive_below_the_docstring_is_not_read(self):
        """It has to sit in the signature, which is where numpydoc reads it."""
        self.write("pkg/m.py",
                   "def apply_where(cond):\n    " + self.BODY.rstrip("\n") +
                   "  # numpydoc ignore=PR02\n")
        self.assertEqual([h["param"] for h in docdrift.scan(self.dir)], ["xp"])

    def test_the_count_is_printed_even_at_zero(self):
        """A dismissal nobody sees is how a report gets cleaner, not more honest."""
        import io
        from contextlib import redirect_stdout
        self.write("pkg/m.py", "def f(a):\n    return a\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            docdrift.print_report(docdrift.scan(self.dir), self.dir)
        self.assertIn("author switched it off: 0", buf.getvalue())

    @unittest.skipUnless(docdrift.numpydoc_available(), "numpydoc not installed")
    def test_both_engines_read_the_same_directive(self):
        """Two engines that disagree about a dismissal cannot be compared."""
        self.write("pkg/m.py",
                   "def apply_where(  # numpydoc ignore=PR01,PR02\n"
                   "    cond, /, *, fill_value=None\n"
                   "):\n    " + self.BODY)
        self.assertEqual(docdrift.scan_numpydoc(self.dir), [])


class TestWhereTheTwoEnginesDisagreed(unittest.TestCase):
    """Every rule here came out of one engine seeing what the other did not.

    Run across the whole Python pool on 6 August 2026, our own parser and
    numpydoc disagreed on 22 findings that were not the reference parser
    swallowing a section heading. Reading them by hand gave four species, three
    of them ours.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, text):
        p = pathlib.Path(self.dir, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_a_heading_underlined_with_equals(self):
        """networkx writes `==========` in nx_latex.py, and we saw no section."""
        self.write("pkg/m.py", '''
def to_latex(Gbunch, pos):
    """Draw it.

    Parameters
    ==========
    Gbunch : NetworkX graph
        The graph.
    sub_latex_labels : dict
        Labels for the subfigures.
    """
    return Gbunch
''')
        self.assertEqual([h["param"] for h in docdrift.scan(self.dir)], ["sub_latex_labels"])

    def test_a_block_indented_deeper_than_its_heading(self):
        """qutip and graphrag indent the whole block one level in.

        Nothing then sits at the heading's indent, and every parameter of the
        function went unread while the report counted the file as checked.
        """
        self.write("pkg/m.py", '''
def plot_energy_levels(H_list, N=0):
    """Plot it.

    Parameters
    ----------

        H_list : List of Qobj
            A list of Hamiltonians.

        h_lables : List of string, optional
            A list of xticklabels for each Hamiltonian
    """
    return H_list
''')
        self.assertEqual([h["param"] for h in docdrift.scan(self.dir)], ["h_lables"])

    def test_the_heading_indent_still_wins_when_something_sits_at_it(self):
        """The fallback is a fallback: an ordinary docstring must not move."""
        self.write("pkg/m.py", '''
def f(a):
    """Do it.

    Parameters
    ----------
    a : int
        The one.
    b : int
        The other.
    """
    return a
''')
        self.assertEqual([h["param"] for h in docdrift.scan(self.dir)], ["b"])

    def test_a_lower_case_section_heading_closes_the_section(self):
        """networkx writes `See also`, and the entry under it became a parameter."""
        self.write("pkg/m.py", '''
def gnm_random_graph(n, m):
    """Make it.

    Parameters
    ----------
    n : int
        Nodes.
    m : int
        Edges.

    See also
    --------
    dense_gnm_random_graph

    """
    return n
''')
        self.assertEqual(docdrift.scan(self.dir), [])

    def test_a_placeholder_in_lower_case_is_no_parameter(self):
        """pyGSTi writes `todo` where the parameters should be."""
        self.write("pkg/m.py", '''
def lsp(x, times):
    """Do it.

    Parameters
    ----------
    todo

    Returns
    -------
    todo
    """
    return x
''')
        self.assertEqual(docdrift.scan(self.dir), [])

    def test_none_written_alone_means_there_are_no_parameters(self):
        """mne writes `None` under Parameters, and it cannot be a name anyway."""
        self.write("pkg/m.py", '''
def load_geometry(self):
    """Load it.

    Parameters
    ----------
    None

    Returns
    -------
    None
    """
    return 1
''')
        self.assertEqual(docdrift.scan(self.dir), [])

    def test_a_name_with_no_description_at_the_end_of_the_section(self):
        """statsmodels documents `tol` alone, and we used to need a description."""
        self.write("pkg/m.py", '''
def cont(self):
    """Continue.

    Parameters
    ----------
    tol

    Returns
    -------
    cont : bool
        Whether to go on.
    """
    return True
''')
        self.assertEqual([h["param"] for h in docdrift.scan(self.dir)], ["tol"])

    def test_a_stray_word_in_the_middle_is_still_no_parameter(self):
        """The guard that was relaxed has to keep holding where it was needed."""
        self.write("pkg/m.py", '''
def f(a):
    """Do it.

    Parameters
    ----------
    a : int
        The one.
    stray
    b : int
        The other.
    """
    return a
''')
        self.assertEqual([h["param"] for h in docdrift.scan(self.dir)], ["b"])


class TestAdapterToTheReferenceParser(unittest.TestCase):
    """The reference parser puts prose in the name field, and we split it.

    Second time in two days: case 75 was a name split on commas without cutting
    at the colon, this is a sentence split on commas. The tool that reads
    another tool is a tool, and it is wrong in the same species.
    """

    def test_a_sentence_is_not_a_list_of_names(self):
        self.assertEqual(
            docdrift.split_names("Must contain ECoG, sEEG or DBS channels"), [])

    def test_a_citation_list_is_not_a_list_of_names(self):
        self.assertEqual(
            docdrift.split_names("Gilbert, Elmer G., Daniel W. Johnson"), [])

    def test_a_real_list_of_names_still_splits(self):
        self.assertEqual(docdrift.split_names("copy, deep, inplace"),
                         ["copy", "deep", "inplace"])

    def test_a_single_name_survives(self):
        self.assertEqual(docdrift.split_names("random_state"), ["random_state"])



class TestFoundOnTheGoogleStyleSweep(unittest.TestCase):
    """Five defects a run over keras, flax and adk-python turned up, 20.08.2026.

    106 findings came back and 57 of them were ours. Forty were the first test
    below, one mistake, and it was hiding behind a full stop.
    """

    def test_a_backtick_behind_a_full_stop_is_stripped(self):
        """40 of 57 false positives in one run.

        "Defaults to `-1`." leaves the closing backtick under the stop. Strip
        quotes first and the backtick survives; strip the stop afterwards and
        nothing revisits it. Same shape as the quote hiding under a bracket in
        linkdrift.
        """
        self.assertEqual(docdrift.norm("`-1`."), "-1")
        self.assertEqual(docdrift.norm("``False``."), "False")
        self.assertEqual(docdrift.norm('"5".'), "5")

    def test_default_to_without_the_s_reads_the_value(self):
        """keras/src/ops/nn.py writes "Default to `-1`."

        The generic branch matched `default` plus a separator and read the
        value as `to`.
        """
        h = findings_in('''
def f(axis=-1):
    """Does the thing.

    Args:
        axis: which one. Default to `1`.
    """
''')
        self.assertEqual([(x["kind"], x.get("param")) for x in h], [("B", "axis")])

    def test_a_docstring_inside_a_fence_is_not_read(self):
        """adk-python shows a prompt template in triple backticks, and the
        template carries a whole docstring of its own with its own Args."""
        self.assertEqual(findings_in('''
def f(prompt):
    """Does the thing.

    Args:
        prompt: the template to use, shaped like this:

            ```
            def tool(timezone, location):
                """Gets the time.

                Args:
                    timezone: the zone
                    location: the place
                """
            ```
    """
'''), [])

    def test_a_summary_line_saying_args_does_not_become_a_parameter(self):
        """A summary line that is itself the word Args with a colon.

        `application_integration_toolset.py` opens that way and carries the
        real section below it. The nested heading parsed as an argument
        literally named Args.
        """
        h = findings_in('''
def f(name):
    """Args:

    Args:
        name: the name
    """
''')
        self.assertEqual(h, [])

    def test_a_samples_directory_is_skipped(self):
        """adk-python keeps demo agents in contributing/samples, where a line
        like `Example: { "location_name": "Basel" }` sits at parameter indent."""
        self.assertIn("samples", docdrift.SKIP_DIRS)


class TestGuardLens(unittest.TestCase):
    """Severity, added 20.08.2026 after a sweep over 59 repositories.

    The sweep is the reason this is a lens and not a tool: 607 findings, 3 with
    a guard-shaped name, 2 of them real. Four probes of what docdrift cannot
    see returned zero security findings between them.
    """

    def test_a_guard_name_is_marked(self):
        for name in ("safe_mode", "verify", "check_hostname", "weights_only",
                     "allow_unsafe", "ssl_verify"):
            self.assertTrue(docdrift.guards_something(name), name)

    def test_an_ordinary_name_is_not(self):
        for name in ("model", "num_workers", "axis", "filters", "batch_size"):
            self.assertFalse(docdrift.guards_something(name), name)

    def test_checkpoint_is_not_a_check(self):
        """The trap that makes this a split rather than a substring match.

        `check` has to fire on `check_hostname` and stay quiet on `checkpoint`,
        which is ordinary in this corpus and guards nothing at all.
        """
        self.assertTrue(docdrift.guards_something("check_hostname"))
        self.assertFalse(docdrift.guards_something("checkpoint"))
        self.assertFalse(docdrift.guards_something("checkpointing"))

    def test_the_keras_line_that_started_this(self):
        """serialization_lib documented safe_mode as defaulting to False.

        It defaults to True. The sentence says the protection against unsafe
        lambda deserialization is off, and goes on to explain how to turn it
        off further.
        """
        h = findings_in('''
def deserialize(config, safe_mode=True):
    """Does the thing.

    Args:
        safe_mode: Boolean, defaults to False. If True, disables unsafe
            lambda deserialization.
    """
''')
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["param"], "safe_mode")
        self.assertTrue(docdrift.guards_something(h[0]["param"]))

if __name__ == "__main__":
    unittest.main(verbosity=2)
