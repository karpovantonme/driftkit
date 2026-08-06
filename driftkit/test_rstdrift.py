#!/usr/bin/env python3
"""Tests for rstdrift.py: parameter fields against signatures. Run: python3 test_rstdrift.py

Every case below is a place the check was actually run against, not an
invented one. The file and project each came from is named in the test.
"""

import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rstdrift


def findings_in(code):
    """A run over one file without building a tree on disk."""
    for k in rstdrift.COUNTS:
        rstdrift.COUNTS[k] = 0
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "module.py"
    p.write_text(code, encoding="utf-8")
    try:
        return rstdrift.scan_file(p, "sample")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def hard_in(code):
    return [h for h in findings_in(code) if rstdrift.is_hard(h)]


def soft_in(code):
    return [h for h in findings_in(code) if not rstdrift.is_hard(h)]


# --------------------------------------------------------------- the two forms


class TestDialects(unittest.TestCase):
    def test_sphinx_form(self):
        """certbot: `_get_valid_int_ans(max_)` documented `max`."""
        hits = hard_in('''
def f(max_):
    """Get a numerical selection.

    :param int max: The maximum entry, must be positive
    """
''')
        self.assertEqual([h["documented"] for h in hits], ["max"])

    def test_epytext_form(self):
        """Twisted writes @param, not :param. Reading only `:` found 8
        callables in the whole tree instead of 2662."""
        hits = hard_in('''
def recvfd(socketfd):
    """Receive a file descriptor.

    @param fd: C{int}
    """
''')
        self.assertEqual([h["documented"] for h in hits], ["fd"])

    def test_type_first_is_read_name_last(self):
        """salt: `:param func str:` has type and name the wrong way round, so
        the name reads as `str`. Sphinx renders it that way too."""
        hits = hard_in('''
class LoadedFunc:
    """Wrap a loaded function.

    :param func str: The function name to wrap
    """
    def __init__(self, name, loader):
        pass
''')
        self.assertEqual([h["documented"] for h in hits], ["str"])

    def test_type_with_no_name(self):
        """salt: `:param dict: The salt options` names no argument at all."""
        hits = hard_in('''
class Master:
    """A master server.

    :param dict: The salt options
    """
    def __init__(self, opts):
        pass
''')
        self.assertEqual([h["documented"] for h in hits], ["dict"])


# ------------------------------------------------------------------- quiet on


class TestQuiet(unittest.TestCase):
    def test_matching_name(self):
        self.assertEqual(hard_in('''
def f(timeout):
    """:param timeout: seconds to wait"""
'''), [])

    def test_kwargs_is_skipped_outright(self):
        """With **kwargs anything may legitimately be documented."""
        self.assertEqual(findings_in('''
def f(a, **kwargs):
    """:param a: first
    :param anything: passed through
    """
'''), [])

    def test_self_and_cls_stay_in_the_signature(self):
        """Dropping them made a module-level decorator whose real argument is
        `cls` into a finding. Both forms below are quiet."""
        self.assertEqual(hard_in('''
def vector_store_connection(cls):
    """:param cls: The class to be decorated."""
'''), [])
        self.assertEqual(hard_in('''
class A:
    def m(self, x):
        """:param self: the instance
        :param x: a value
        """
'''), [])

    def test_overload_stub_is_not_the_signature(self):
        """The first `__init__` is a narrower stub; the implementation is last."""
        self.assertEqual(hard_in('''
from typing import overload

class Getter:
    """:param selector: a selector
    :param expect_type: the expected type
    """
    @overload
    def __init__(self, selector): ...
    def __init__(self, selector, expect_type=None):
        pass
'''), [])


# ---------------------------------------------------------------------- soft


class TestSoft(unittest.TestCase):
    def test_args_passthrough_is_soft_not_hard(self):
        """Pillow `Image.eval(image, *args)` documents `:param function:`.
        The name is not in the signature, but it is what the caller passes
        second and the entry is the only place that says so."""
        code = '''
def eval(image, *args):
    """Apply the function to each pixel.

    :param image: The input image.
    :param function: A function object, taking one integer argument.
    """
    return image.point(args[0])
'''
        self.assertEqual(hard_in(code), [])
        self.assertEqual([h["documented"] for h in soft_in(code)], ["function"])


# ------------------------------------------------------------------ reporting


class TestContract(unittest.TestCase):
    def test_finding_carries_the_shared_keys(self):
        h = hard_in('''
def f(a):
    """:param b: not an argument"""
''')[0]
        for key in ("repo", "path", "line", "symbol", "documented", "declared", "hard"):
            self.assertIn(key, h)

    def test_line_points_at_the_field(self):
        """The reported line is what the fix script edits, so it has to be the
        field itself and not the top of the docstring."""
        code = '''
def f(a):
    """Summary.

    :param a: fine
    :param b: not an argument
    """
'''
        hits = hard_in(code)
        line = code.splitlines()[hits[0]["line"] - 1]
        self.assertIn(":param b:", line)

    def test_coverage_counts_callables_not_findings(self):
        """A tree with no findings must still report what was looked at:
        `0 findings` next to `0 callables` means the dialect is wrong, not
        that the project is clean."""
        findings_in('''
def f(a):
    """:param a: fine"""

def g(b):
    """:param b: fine"""
''')
        self.assertEqual(rstdrift.COUNTS["callables"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
