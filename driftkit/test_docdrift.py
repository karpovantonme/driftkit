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

    def test_google_style_is_not_parsed(self):
        """Stated in the header: numpydoc only."""
        self.assertEqual(findings_in('''
def f(model):
    """Does the thing.

    Args:
        MODEL: the model
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
