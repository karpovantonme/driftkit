# False positives journal

This is not documentation about the tool. It is part of the tool.

## Why this file exists

Every rule in `docdrift` came from a case where it was wrong on a real project. Nothing here was invented at a desk: something was reported, read by hand, understood, and only then turned into a rule and a test.

Three practical reasons to keep the list in the open.

**One.** The honest answer to "is it accurate?" is that accuracy here is not a property of the algorithm, it is **accumulated reading**. It depends on the language, the conventions of the project, the decorators in use, the way defaults are written. A tool that claims a precision number without a list like this has either not been run for real or has not been counting.

**Two.** This is a vaccination record. It shows which mutations the tool is already immune to and, by omission, which it is not. An empty row under "where else this can happen" is not a guarantee, it is an absence of knowledge.

**Three.** The mistakes repeat by **family**, not one at a time. A truncated value matched a real one four separate times, in different places and for different reasons. Until they sit in one table each looks like bad luck.

## How to read it

**Cost** is how many false findings the case produced at the moment it was discovered. Not an estimate, a count: that many lines would have gone to maintainers if nobody had read them.

**Signal** is how you recognise it in a new project.

---

| # | What was taken for a defect | Cost | Signal | What removed it |
|---|---|---|---|---|
| 1 | Docstring of a `@property` describes the object it returns. In networkx `G.edges` is a `cached_property` returning a view called as `G.edges(nbunch, data)`, so `nbunch` and `data` belong in the docstring and cannot be in the signature | **41 of 44** on networkx | Decorated with `property` or `cached_property`, docstring names arguments the function itself has none of | Properties are not judged for class A |
| 2 | A `Parameters` section inside doctest output. A doctest prints another function's docstring in full, without indentation, so its sections look like the host's own | 2 on mne-python | The names come from a block below `Examples` | Docstring is cut at the `Examples` section |
| 3 | `default 0.01` truncated at the first dot and read as `0` | **102** across five projects | Every fractional default in the project reports as a mismatch | The value ends at a sentence boundary or a comma, not at the first dot |
| 4 | Sentinel `None`: the code has `None`, the docstring says what it becomes, e.g. `default: nx.Graph` | **80 of 168** on networkx | Code default is `None`, documented default is anything else | If the code says `None` and the docs do not, stay quiet |
| 5 | Prose instead of a value: `default: all nodes in G`, `default: len(G)` | part of the same 168 | The documented default contains a space or unbalanced brackets | Prose is detected and not compared |
| 6 | Same number in two notations: `0o775` in the docs against `509` from `literal_eval`, `1e-8` against `1e-08` | 16 on pyTMD | Both sides parse as numbers and are equal | Numbers are compared as numbers. Booleans excluded, so `True` never matches `1` |
| 7 | `@deprecate_kwarg("random_state", "rng")` keeps accepting the old name. The docstring documents both and marks the old one deprecated; the signature knows nothing about it | **30 of 56** on statsmodels | A decorator whose name contains deprecat/renam/alias/compat | Names a compatibility decorator spells out are added to the signature |
| 8 | The same, but the decorator does **not** spell the names out: bare `@deprecated`, or names taken from an external dict | unknown by construction | A compatibility decorator with no string arguments | That function is not judged at all, and the count of such skips is printed. Claiming "this name does not exist" from a partially known source is the mistake itself |
| 9 | `TODO: looks like not used yet` inside a `Parameters` section, shaped exactly like `name : type` | 1 on statsmodels | The "name" is TODO, FIXME, XXX and friends | Note keywords are excluded from parameter names |

## Two more, from the machinery around it

| # | What happened | Cost | What removed it |
|---|---|---|---|
| 10 | The confidence flag was called `confident` in one tool and `hard` in the others. The runner read `hard` defaulting to True and counted **soft findings as hard** | silent, everywhere | A written contract plus a conformance test |
| 11 | The self-refutation step read a finding's location only as a `path:line` string, so findings that carry `file` and `line` as separate fields **were never checked at all** | all 19 docdrift findings in one run | Both forms are read |

Case 11 is worth a second look. It made the report *cleaner*: fewer findings survived, and the output looked better for it. That is the dangerous shape of error in this kind of tool, and it is why every report ends with a coverage block saying how much was examined, not just how much was found.

## Bring a case

If the tool is wrong on your project, an issue with the file, the line, what it reported and what is actually there is the most useful thing you can send. Everything above got here that way.
