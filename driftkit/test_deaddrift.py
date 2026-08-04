#!/usr/bin/env python3
"""Tests for deaddrift.py: removed, and still promised.

Positive control is the historical etcd case before our pull request #22244: the
`#### Flags Removed` section of the 3.6 changelog listed six proxy settings while
the sample configuration kept offering them. The clone has it fixed by now, so a
live run stays quiet and the case is reconstructed synthetically.

Classes of lie removed during development on live projects:

  4 of 4 on etcd. The `Breaking Changes` section lists additions, renames and
    default changes as well as removals.

  9 of 9 on rclone. The line "Remove spurious error message on `--sftp-...`"
    removes an error message rather than a flag. The name has to sit next to the
    verb.

  14,022 on karmada. A speculative "reverse form" pattern (`name ... removed`)
    matched almost any line. Deleted entirely.

  25 on rclone. The window cut a name in half and the stump `--sftp-disab`
    matched as a substring across the whole documentation.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import deaddrift as dd  # noqa: E402

ETCD = os.path.expanduser("~/Projects/oss/etcd")
HAS_ETCD = os.path.isdir(ETCD)


def project(files: dict) -> str:
    root = tempfile.mkdtemp()
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(body)
    return root


def run(files: dict) -> dd.Report:
    rep = dd.Report()
    dd.analyse(project(files), rep)
    return rep


def hard(rep):
    return [f for f in rep.findings if f.hard]


CHANGELOG = """# Changelog 3.6

### Breaking Changes

- `etcd` no longer serves client requests on `--listen-peer-urls`.
- Applications must be built with `--go-version-check` enabled.

#### Flags Removed

- The following flags have been removed:

  - `--proxy`
  - `--proxy-failure-wait`
  - `--proxy-refresh-interval`
"""


class TestKnownCase(unittest.TestCase):
    """etcd before pull request #22244."""

    FILES = {
        "CHANGELOG/CHANGELOG-3.6.md": CHANGELOG,
        "etcd.conf.yml.sample": (
            "# Proxy settings\n"
            "--proxy: 'off'\n"
            "--proxy-failure-wait: 5000\n"
        ),
    }

    def test_removed_flag_still_offered_by_the_config_sample(self):
        rep = run(self.FILES)
        h = hard(rep)
        self.assertEqual({f.name for f in h}, {"--proxy", "--proxy-failure-wait"})

    def test_finding_carries_both_coordinates(self):
        f = hard(run(self.FILES))[0]
        self.assertRegex(f.where, r"^.+:\d+$")
        self.assertRegex(f.changelog_ref, r"CHANGELOG.+:\d+$")
        self.assertIn("Removed", f.section)

    def test_breaking_changes_section_is_not_a_source(self):
        """The Breaking Changes section gave all four false findings on etcd."""
        rep = run(self.FILES)
        self.assertNotIn("--listen-peer-urls", rep.removed_names)
        self.assertNotIn("--go-version-check", rep.removed_names)


class TestSilence(unittest.TestCase):
    def test_mention_qualified_by_version(self):
        """In Prometheus every mention of a removed flag is honestly qualified by
        version. That is correct documentation."""
        rep = run(
            {
                "CHANGELOG.md": CHANGELOG,
                "docs/config.md": (
                    "## Legacy options\n\n"
                    "For etcd versions v3.5 and below you could set `--proxy` here.\n"
                ),
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.qualified)

    def test_old_documentation_directory(self):
        rep = run({"CHANGELOG.md": CHANGELOG, "docs/v3.5/config.md": "Set `--proxy` to on.\n"})
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.old_docs)

    def test_postmortem_and_versioned_readme(self):
        rep = run(
            {
                "CHANGELOG.md": CHANGELOG,
                "Documentation/postmortems/incident.md": "started with `--proxy`\n",
                "etcdctl/READMEv2.md": "### --proxy\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertGreaterEqual(len(rep.old_docs), 2)

    def test_changelog_itself_is_not_searched(self):
        rep = run({"CHANGELOG.md": CHANGELOG})
        self.assertEqual(rep.findings, [])

    def test_name_still_present_in_code_is_soft(self):
        rep = run(
            {
                "CHANGELOG.md": CHANGELOG,
                "docs/config.md": "Set `--proxy` in your config.\n",
                "server/config.go": 'fs.String("--proxy", "", "proxy mode")\n',
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.still_in_code)

    def test_removal_of_something_about_a_flag_is_not_removal_of_the_flag(self):
        """rclone: «Remove spurious error message on `--sftp-disable-concurrent-reads`»
        removes the message rather than the flag. All nine "findings" of nine."""
        rep = run(
            {
                "CHANGELOG.md": (
                    "# Changelog\n\n"
                    "- Remove spurious error message on `--sftp-disable-concurrent-reads` (Nick)\n"
                ),
                "docs/sftp.md": "#### --sftp-disable-concurrent-reads\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertNotIn("--sftp-disable-concurrent-reads", rep.removed_names)
        self.assertTrue(rep.far_from_verb)

    def test_short_bullet_form_still_works(self):
        rep = run(
            {
                "CHANGELOG.md": "# Changelog\n\n- Removed `--dump-auth` flag\n",
                "docs/sftp.md": "Use `--dump-auth` to see the auth exchange.\n",
            }
        )
        self.assertEqual([f.name for f in hard(rep)], ["--dump-auth"])


class TestNfCoreLessons(unittest.TestCase):
    """Five mechanisms of lying removed during a mass sweep.
    Raw result: 157 hard findings across 58 projects, none of them real."""

    def test_deprecated_is_not_removed(self):
        """A deprecated option still works and documenting it is correct.
        Deprecated sections gave 27 false findings of 81 on sarek alone."""
        rep = run(
            {
                "CHANGELOG.md": "# c\n\n### Deprecated\n\n- `--old_flag` is deprecated\n",
                "docs/a.md": "Use `--old_flag` for legacy runs.\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertNotIn("--old_flag", rep.removed_names)

    def test_successor_name_is_alive(self):
        """"`--skip_qc` is now `--skip_tools`": the first is gone, the second is
        alive and appears in the documentation 8 times."""
        rep = run(
            {
                "CHANGELOG.md": "# c\n\n### Removed\n\n- `--skip_qc` is now `--skip_tools`\n",
                "docs/a.md": "Set `--skip_tools` to skip stages.\n",
            }
        )
        self.assertEqual(hard(rep), [])
        # the successor must not enter the removed list at all
        self.assertNotIn("--skip_tools", rep.removed_names)
        self.assertIn("--skip_qc", rep.removed_names)

    def test_scoped_removal_is_not_removal_of_the_name(self):
        """"Restart from `--step annotate` from folder is removed" removes a
        scenario rather than the parameter."""
        rep = run(
            {
                "CHANGELOG.md": "# c\n\n### Removed\n\n- Restart from `--step annotate` from folder is removed\n",
                "docs/a.md": "Pass `--step` to choose the stage.\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.scoped)

    def test_camelcase_flag_is_not_truncated(self):
        """Without capitals `--genomeDict` truncated to `--genome`, which is a
        live parameter. The third case of one family: a name truncated by the
        parser matching a real one."""
        rep = run(
            {
                "CHANGELOG.md": "# c\n\n### Removed\n\n- `--genomeDict` has been removed\n",
                "docs/a.md": "Set `--genome` to pick the reference.\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertIn("--genomeDict", rep.removed_names)
        self.assertNotIn("--genome", rep.removed_names)

    def test_name_mentioned_later_than_its_removal_is_alive(self):
        """A changelog runs from newer versions to older ones. A mention ABOVE the
        removal line is later, so the name came back or never left. In sarek
        `--step` was listed as removed in a 2019 entry."""
        rep = run(
            {
                "CHANGELOG.md": (
                    "# c\n\n## [3.0]\n\n### Added\n\n- `--step` now accepts a list\n\n"
                    "## [1.0]\n\n### Removed\n\n- `--step` in `annotate.nf`\n"
                ),
                "docs/a.md": "Pass `--step` to choose the stage.\n",
            }
        )
        self.assertEqual(hard(rep), [])
        self.assertTrue(rep.resurrected)


class TestWholeNameMatching(unittest.TestCase):
    def test_prefix_flag_does_not_match_longer_flag(self):
        """`--dump` must not match `--dump-headers`, otherwise one finding
        multiplies across the whole documentation."""
        rep = run(
            {
                "CHANGELOG.md": "# Changelog\n\n- Removed `--dump-auth` option\n",
                "docs/a.md": "Use `--dump-auth-extended` for more.\n",
            }
        )
        self.assertEqual(hard(rep), [])

    def test_name_is_not_truncated_by_the_window(self):
        """The stump `--sftp-disab` matched as a substring everywhere: 25 false."""
        rep = run(
            {
                "CHANGELOG.md": "# Changelog\n\n- Removed `--sftp-disable-concurrent-reads` option\n",
                "docs/a.md": "#### --sftp-disable-concurrent-reads\n",
            }
        )
        names = {f.name for f in hard(rep)}
        self.assertEqual(names, {"--sftp-disable-concurrent-reads"})


@unittest.skipUnless(HAS_ETCD, "no etcd clone")
class TestOnRealEtcd(unittest.TestCase):
    """Negative control: after our pull request #22244 there are no hard findings."""

    @classmethod
    def setUpClass(cls):
        cls.rep = dd.Report()
        dd.analyse(ETCD, cls.rep)

    def test_no_hard_findings_after_the_fix(self):
        self.assertEqual(
            [f"{f.name} {f.where}" for f in hard(self.rep)], [], "a hard finding appeared"
        )

    def test_work_was_actually_done(self):
        self.assertGreater(len(self.rep.changelogs), 3)
        self.assertGreater(self.rep.sections, 0)
        self.assertGreater(self.rep.files_searched, 100)
        self.assertGreater(len(self.rep.removed_names), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
