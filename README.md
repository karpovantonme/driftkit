# driftkit

Tools that compare what a project **says** against what it **does**.

Every check here follows the same shape: find two statements about the same thing, in two places, and report where they disagree. A docstring against the signature below it. A workflow matrix against the versions in the classifiers. A copied file against the upstream it was copied from.

The first tool in the kit is `docdrift`. It compares numpydoc `Parameters` blocks against the actual signature, parsed with `ast`.

```console
$ python3 driftkit/docdrift.py ~/src/statsmodels

=== Documented name not in the signature (16) ===

  ~/src/statsmodels/archive/descstats.py:20  descstats()
    docstring:  v
    code:       data, cols, axis

  ...

=== Coverage ===
  tree:                   ~/src/statsmodels
  files read:             522
  functions with Parameters: 2339
  properties skipped:     293 (docstring describes what they return)
  findings:               16 hard, 3 soft
  run:                    docdrift.py fingerprint 17f92143, 2026-08-04 23:08
```

That is a real run, not an illustration. Eleven of those sixteen sit in `archive/`, which is why the tool prints where it looked rather than a verdict: **the last decision is yours, and the report is built to be argued with.**

No install, no config. Python 3.9+, standard library only.

---

## Where it lies

This section is first on purpose.

A tool like this is only as good as the list of mistakes already worked out of it, and that list is the actual product. Precision here is not a property of the algorithm, it is **accumulated reading**. Every entry below came from a real project, was read by hand, and only then became a rule and a test.

| What was reported as a defect | Cost when found | What removed it |
|---|---|---|
| Docstring of a `@property` describes the object it **returns**, not itself. In networkx `G.edges` is a cached_property returning a view called as `G.edges(nbunch, data)` | **41 of 44 findings** on networkx | Properties are skipped |
| `@deprecate_kwarg("random_state", "rng")` keeps accepting the old name; the docstring documents both, the signature knows nothing | **30 of 56** on statsmodels | Names a compatibility decorator spells out are added to the signature |
| A decorator that adds names **without naming them** (bare `@deprecated`, names from an external dict) | unknown, by construction | That function is not judged at all, and the report prints how many were skipped this way |
| A doctest prints someone else's docstring, so a `Parameters` section appears inside example output | several per project | Everything from `Examples` onwards is cut |
| `default 0.01` truncated at the first dot and read as `0` | **102 false** across five projects | Value ends at a sentence boundary or a comma |
| Sentinel `None` in the code, docstring says what it becomes (`default: nx.Graph`) | **80 of 168** on networkx | Sentinel defaults are not compared |
| Same number in two notations: `0o775` in the docs, `509` from `literal_eval` | 1 | Numbers compared as numbers |
| Prose instead of a value: `default: all nodes in G` | many | Prose is detected and skipped |
| `TODO: looks like not used yet` inside a `Parameters` section parses as `name : type` | 1 | Note keywords are excluded |

Eleven worked cases for this tool, with the reasoning behind each rule: [FALSE-POSITIVES.md](FALSE-POSITIVES.md). The wider kit has 61 so far; they arrive here with their tools.

### What to do when it lies

**On your side, before you act on a finding.** Four checks catch almost everything the tool still gets wrong, and each takes seconds:

1. **Look at the lines above the definition.** A compatibility decorator (`@deprecate_kwarg`, `@renamed`, `@deprecated_alias`) adds names the signature never mentions. The tool handles the ones that spell their names out, but not every project spells them out.
2. **Look at where the file lives.** `archive/`, `sandbox/`, vendored trees. The code may be genuinely wrong there and genuinely not worth a pull request.
3. **Look for an opt-out on the line itself,** like `# numpydoc ignore=PR01`. Somebody already decided this one is fine.
4. **After you fix anything, run it again.** Not to admire the zero, but because a rename applied by search and replace lands in the first match, which is often a neighbouring overload that was correct. That has happened here twice in one afternoon.

**On this side, when a case turns out to be real.** The cycle is short and it is the whole method:

- the case is read by hand on the live code, until the mechanism is understood, not just the symptom;
- a rule is written **only from what the case showed**. Rules extrapolated a step further, on the theory that "if this happens then surely the opposite happens too", cost 14 022 and 162 false findings in two attempts here. Generalising sideways is fine, guessing forward is not;
- the rule becomes a test, so it cannot quietly come back;
- the case joins the journal with the number it cost, because the count is what makes the next person believe the list.

**If it lies to you, please open an issue with the case.** A file, a line, what it said, what is actually there. Everything in the table above arrived that way.

## Where it stays quiet, and why that matters

A tool that never reports zero has not been calibrated.

- **etcd, 0 findings.** Correct: their swagger is generated from proto, so the two cannot disagree by construction.
- **Boost.Histogram, 0 after a parser fix.** All 11 earlier findings were `decltype(auto)` misread as an argument list.
- **AFL++, 0.** The one real mismatch there was fixed by [a pull request from this tool](https://github.com/AFLplusplus/AFLplusplus/pull/2865), and it correctly says so now.

The `Coverage` block at the end of every run exists for the same reason: `findings: 0 hard` next to `functions with Parameters: 0` means "nothing to compare", not "clean". Those are different answers and the report keeps them apart.

## Findings that became merged pull requests

Not examples written for a README. Real ones, with the maintainer who merged them.

| Project | What it found | Result |
|---|---|---|
| [MNE-Python](https://github.com/mne-tools/mne-python/pull/14125) | signature had no `opacity`, docstring described `backface_culling`, which does not exist | merged in 11 h |
| [MontePy](https://github.com/idaholab/MontePy/pull/1003) | four docstrings naming parameters that are gone | merged in 14 h |
| [Boost.GIL](https://github.com/boostorg/gil/pull/792) | 11 `\param` names against the declarations | merged in 18 h |
| [Boost.Algorithm](https://github.com/boostorg/algorithm/pull/131) | `is_permutation` documents `last2`, a local variable inside the body | merged in 15 h by the library author |
| [statsmodels](https://github.com/statsmodels/statsmodels/pull/10028) | five parameters documented but not accepted, incl. two copied from a neighbouring overload | open |
| [MNE-Python](https://github.com/mne-tools/mne-python/pull/14134) | ten renames, plus `max_iter` documented as `1000` where the code says `15` | open |

## Known blind spots

- **numpydoc only.** Google style (`Args:`) is not parsed at all.
- Functions with `*args`/`**kwargs` are skipped for the "name not in signature" check.
- Defaults computed by an expression rather than a literal are skipped.
- Test, example and documentation directories are not read.

## Contract

Every tool in the kit takes `--json FILE` and `-v`, writes objects carrying a boolean `hard`, ends its report with a `=== Coverage ===` block, and **exits 1 if and only if there is at least one hard finding**, so it can be used directly in a shell `if`. See [driftkit/common.py](driftkit/common.py).

## Tests

```console
$ PYTHONPATH=driftkit python3 -m pytest tests/ -q
31 passed
```

Most of them are regression tests for the table above: each one pins a false positive that used to happen.

## What is coming

The kit has ten more detectors in the same shape, currently used privately: docstrings against declarations in C++ Doxygen, protobuf against OpenAPI, a vendored copy against the upstream that has since been fixed, translated docs against the original, declared Python support against the CI matrix, dead external links, helm-unittest assertions that never compare their message. They are being translated and will land here one at a time, each with its own section of the false positives journal.

## License

MIT. See [LICENSE](LICENSE).
