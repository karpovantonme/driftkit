# False positives: the vaccination record of this kit

*This is not documentation about the tools. It is part of the tools.*

## Why this file exists

The kit, eleven detectors and five pipeline stages, was not written out of lucky
guesses. It was written out of **reading the places where it lied**. Sixty-one
worked cases so far, and not one of them came from imagination: every one turned
up on a live project, was read by hand, and only then became a rule and a test.

The file earns its place three times over, and all three are practical.

**First.** The honest answer to "is it accurate?" is that accuracy here is no
property of an algorithm. It is **the accumulated list of what has already been
read through**. Everything depends on the language, the build system, the
version, the conventions of the project. A tool that claims accuracy without such
a list either never ran for real or never counted.

**Second.** This is a vaccination record. It shows **which mutations the kit is
already immunised against and which it is not**. A new project brings a new
mutation, the mutation lands here, becomes a test, and from that point the kit is
protected. An empty line in the "not immunised" table is ignorance rather than a
guarantee.

**Third.** The mistakes of a tool repeat in **species** rather than one by one. A
name truncated by a parser matched a real name four times in a row, in different
places and for different reasons. Until that is collected in one file, each
occurrence looks like an accident.

## How to read the table

**Cost** is how many false findings the case produced at the moment it was found.
That is a measurement rather than an estimate: this many lines would have been
sent to maintainers if nobody had read them.

---

## The table

| № | Tool | What was reported as a defect | Cost | What removed it |
|---|---|---|---|---|
| 1 | ifacedrift | Different nesting depth between two formats | **115 instead of 9** | Zero shared fields is one trouble, not N |
| 2 | ifacedrift | Response envelope: `time`, `usage` around `result` | 2 | Envelope names collected from `paths.*.responses` |
| 3 | ifacedrift | Type discriminator `"type": "text"` | 9 | Reference to a single-value enum |
| 4 | ifacedrift | Different depth: `points_selector` against `points`+`filter` | 3 | A check for one level deeper on the other side |
| 5 | ifacedrift | Union member: `DatetimeRange` inside `RangeInterface` | 1 | Union members collected from the schema |
| 6 | liftdrift | One declaration marked twice | 161 instead of 123 | Merge by declaration coordinate |
| 7 | liftdrift | A merge commit carries the same diff | 4 | Skip everything with a parent count other than 1 |
| 8 | liftdrift | The fixed code is absent from the copy | 11 instead of 3 | Require the removed lines to be present |
| 9 | transdrift | A translated comment inside a code example | **166 of 170** | Strip full-line and trailing comments |
| 10 | transdrift | The space after `//` is optional | part of the same 166 | Marker without a required space |
| 11 | transdrift | mermaid diagrams: node labels are prose | 3 | A list of prose languages |
| 12 | transdrift | A two-line block | 2 | Minimum meaningful lines per block |
| 13 | transdrift | A link to the local locale `/ja/docs/x` | many | Strip the locale prefix |
| 14 | transdrift | A scatter of losses on one page | 89 -> 50 | Folded into one `page-behind` finding |
| 15 | gitdrift | A "walker" was any function touching the fields | **35 instead of 0** | Require mirrored assignment |
| 16 | gitdrift | Sibling structs sharing field names | part of the same 35 | The function has to name the type |
| 17 | gitdrift | Fields nobody copies by design | n/a | Excluded BEFORE any counting |
| 18 | gitdrift | `_ Kind = iota` taken for a value | 12 | Skip the blank identifier |
| 19 | gitdrift | Generated files | 12 | `Code generated ... DO NOT EDIT` |
| 20 | supportdrift | `go.mod` states a language minimum, not a CI promise | 3 | Go excluded from the minimum check |
| 21 | supportdrift | A matrix behind `${{ env.MIN_PYTHON }}` | 2 | No negative claims from an incomplete source |
| 22 | namedrift | Insertion and deletion of a character | **162** | Transposition and substitution only |
| 23 | namedrift | Substitution inside an abbreviation: `EBX` against `RBX` | 18 of 19 | Differing segment of 5 characters or more |
| 24 | namedrift | A name invisible to users | 1 | Context: documentation, string, identifier |
| 25 | deaddrift | The `Breaking Changes` section | 4 of 4 | Removal sections only |
| 26 | deaddrift | "Remove an error message about a flag" | 9 of 9 | Name next to the verb, 15-character window |
| 27 | deaddrift | An invented "reverse form" pattern | **14,022** | The rule was deleted entirely |
| 28 | deaddrift | The window cut a name in half | 25 | Search the full line |
| 29 | deaddrift | `Deprecated` equated with `Removed` | 27 of 81 | A deprecated option still works |
| 30 | deaddrift | A successor name taken from "X is now Y" | part of the same 81 | Take only what is left of the marker |
| 31 | deaddrift | A scenario was removed rather than the name | part of the same 81 | The words restart, support, option to |
| 32 | deaddrift | `--genomeDict` truncated to `--genome` | 30 | Capitals added to the pattern |
| 33 | deaddrift | A flag with a dot, `--request.logging-config` | 14 | Dot in the pattern plus a general truncation guard |
| 34 | deaddrift | A name removed in 2019 and brought back | part of the same 30 | A mention above the removal line |
| 35 | assertdrift | `template`, `documentIndex` are legitimate siblings | 85 | A list of assertion modifiers |
| 36 | linkdrift | 403 taken for a dead link | part of 123 | 403/401/429 mean not let in, the page is alive |
| 37 | linkdrift | A timeout taken for death | part of 123 | A second pass, otherwise "unverified" |
| 38 | sitecheck | "DO NOT **create** a PR" not recognised | 1 project | A wider phrasing |
| 39 | sitecheck | The AI policy is a section rather than a file | 1 project | Search for the section heading |
| 40 | sitecheck | An AI section taken for a ban | **rclone, where we have a merge** | Judge by the requirements in the body |
| 41 | sitecheck | The first reason masked the second | 1 project | The verdict names every reason |
| 42 | refute | Bare version numbers taken for a caveat | **killed a real finding** | A caveat is language rather than a number |
| 43 | sweep | `.github` dropped as a hidden directory | quietly reduced coverage | An exception in the shared mask |
| 44 | the kit | `confident` against `hard` in JSON | soft findings counted as hard | The contract in `common.py` plus a conformance test |
| 45 | lessons | A checklist from `github-actions[bot]` taken for a maintainer request | 1 (nilearn) | A bot author is dropped before the text is read |
| 46 | lessons | The bare word "changelog" with no request around it | 2 (MontePy, opposite meaning) | A request is caught by a phrase, never by one word |
| 47 | lessons | "@all-contributors please add @author" | 1 (sniffnet, a thank-you) | A reply addressed elsewhere is not our lesson |
| 48 | lessons | One reply arriving from three API endpoints | every lesson tripled | Dedupe by (repo, pull request, quote) |
| 49 | the kit | `gh api --paginate` concatenates objects back to back | a crash on the first live run | Parse a stream of values instead of patching strings |
| 50 | docdrift | `@property`: the docstring describes the returned view | **41 of 44** on networkx | Properties are not judged for class A |
| 51 | docdrift | A `Parameters` section inside doctest output | 2 on mne | The docstring is cut at the Examples section |
| 52 | docdrift | `default 0.01` truncated at the dot to `0` | **102** across five projects | The value ends at a sentence boundary or a comma |
| 53 | docdrift | A sentinel `None` in the code against meaning in the docs | **80 of 168** on networkx | With None in the code and something else in the docs, stay quiet |
| 54 | docdrift | Prose instead of a value: "all nodes in G" | part of the same 168 | A space or an unbalanced parenthesis is no value |
| 55 | docdrift | `0o775` against `509`, `1e-8` against `1e-08` | 16 on pyTMD | Numbers compared as numbers |
| 56 | doxdrift | One comment documenting a family of overloads | **25 of 25** on asio | A repeated `\param` name dismisses the block |
| 57 | buildprobe | `pytest --cov` not recognised as coverage | quietly understated the markers | The short form added to the list |
| 58 | refute | A coordinate split across two fields was unreadable | **all 19 docdrift findings went unchecked** | The split form is read alongside `path:line` |
| 59 | refute | The comment rule applied to a finding ABOUT a comment | **killed 24 real findings of 24** on Boost.Geometry | The rule does not apply to detectors judging documentation |
| 60 | docdrift | `@deprecate_kwarg` accepts a name beyond the signature | **30 of 56** on statsmodels | Names a compatibility decorator spells out are added; an opaque decorator dismisses class A entirely |
| 61 | docdrift | `TODO: looks like not used yet` inside a Parameters section | 1 on statsmodels | Note keywords are no argument names |
| 62 | linkdrift | XML and SAML namespace URIs taken for addresses | **8 of 11** on poweradmin | Identifier URIs are excluded and counted separately |
| 63 | linkdrift | A templated address arrives as a stump: `.../{tenant` | 3 on poweradmin | A single `{` is a template marker, not only the doubled form |
| 64 | linkdrift | `www.example.com` escaped the illustrative-host filter | **10 of 11** on php-curl-class | Any subdomain of a reserved host is allowed |

Cases 62 to 64 were reported from outside, by [@darkdi](https://github.com/darkdi), in the first three issues this repository ever received. All three were correct, all three are now rules with tests. Between them they accounted for **every finding linkdrift produced on two projects**: eleven out of eleven, twice.

Case 63 deserves a note. `}` is excluded from the address pattern, so `https://login.microsoftonline.com/{tenant}/saml2` was captured as `https://login.microsoftonline.com/{tenant`, and that stump then returned 404 honestly. This is the same species as cases 26, 28 and 32 in this table: a value cut short by a parser matching something real. Fourth occurrence, different tool, different language.

Sixty-one worked cases. None invented: every one turned up on a live project.

The numeric summary of cases 50 to 55 is worth stating on its own, because it
shows the price of reading. Five scientific Python projects, networkx, pyTMD,
mne-python, felupe and scikit-image, produced **417 findings** from the raw tool.
After the six rules named above, **29** were left. Not one of those rules was
thought up in advance: each came from reading a specific finding by hand.

Case 58 is the quiet one and the more unpleasant for it. The refuter understood a
coordinate only as the string `path:line`, while docdrift and doxdrift write
`file` and `line` as separate fields. On nineteen findings it honestly printed
"without coordinates: 19 (nothing to refute against)" and checked none of them.
The report looked healthy, and tidy at that. Same as case 43 with the dropped
`.github`: **a false finding shouts while an unchecked project stays silent and
only makes the report look neater.**

Case 49 comes from a different test: a crash rather than a false finding. It is
recorded here because the mechanism carries over, **string patching instead of
parsing**. The old code joined pages by replacing `][` with a comma, which worked
right up to the first response made of objects rather than arrays. Parsing a
stream of values handles both.

---

## Species of tool error

The cases repeat in families. That is the transferable knowledge, and it applies
well beyond this kit.

### A. A name truncated by the parser matches a real one

**Cases 28, 32, 33, 52.** Four times in a row, in two tools, for four different
reasons: a window sliced the line, a pattern lacked capitals, a pattern lacked
the dot, a dot sat in the list of stop characters.

Every time the stump matched a real, live name and produced false findings by the
handful: `--sftp-disab` matched as a substring, `--genome` and `--request` are
live parameters, `0` is a plausible default.

**The rule:** after extracting a name, check that the source line does not
continue it. The check does not care which character was forgotten.

### B. Many mentions of one trouble taken for many troubles

**Cases 1, 14, and the cluster rule in the refuter.** 115 "mismatches" in qdrant
were 9. Seventeen missing links on one page of a Chinese translation were one
stale table.

**The rule:** one dead address repeated in fifty files is one finding. Print the
address, the count and the first three coordinates.

### C. Mentions counted instead of entities

The newcomer count in the project survey counted edits rather than people: 23
"newcomers" in one project turned out to be one person.

**The rule:** count the entity you are actually claiming something about.

### D. A negative claim from an incomplete source

**Cases 21 and 60. The most expensive rule of them all, because it is broken
without a sound, and because it repeated in another tool on another language.**

In nilearn the CI matrix hides behind `${{ env.MIN_PYTHON }}`. The list of
versions was knowingly incomplete, and no claim of "this version is not tested"
could be made from it.

In statsmodels the same happened with the argument list.
`@deprecate_kwarg("random_state", "rng")` means the function **still accepts**
`random_state`; the docstring honestly documents both names while the signature
says nothing about the old one. The scanner read the signature alone and declared
a live name nonexistent: thirty false findings out of fifty-six. The case arrived
from a live run, and it showed that species carry across tools: different
mechanism, one mistake.

Hence the rule in two parts, and the second matters more. Names a decorator
**spells out** are added to the signature. A compatibility decorator that names
nothing, a bare `@deprecated` or names from an external dict, dismisses class A
for that function **entirely**: the source is incomplete, so no negative claim can
stand. The report prints the number of such functions on its own line, otherwise
silence is indistinguishable from cleanliness.

**The subtlety that nearly cost a real finding:** a reference to an already parsed
key (`${{ matrix.python-version }}`) is not opaque. The difference between "could
not expand" and "expanded elsewhere" decides everything.

### E. An invented rule

**Case 27, and it is the most expensive single line in this file.**

The "reverse form" of a removal (`name ... removed`) was written speculatively
rather than derived from a known case. On karmada it produced **14,022** false
findings: almost any line where a name sits near the word removed matches it.

A related case, 22: allowing "insertion of a character" in the typo finder felt
natural and caught families of related flags instead, 162 false.

**The rule:** a rule comes from a case that was read by hand. Never from an idea
of how a defect might look.

### F. A name with no context identifies nothing

**Cases 16, 23, 24, 39, 46.**

In karmada `ProviderInfo`, `RegionInfo` and `ZoneInfo` share five field names, so
a function about one produced findings in all three. In AFL++ `GUM_X86_EBX` and
`GUM_X86_RBX` are the 32-bit and 64-bit registers. In qdrant the AI policy lives
as a section rather than a file. In MontePy the word "changelog" stood in a reply
that **lifted** the changelog requirement.

**The rule:** before judging a name, establish **what it belongs to**. The type
has to be named, the differing segment has to be a word, a section has to have a
body.

### G. A different form taken for a different meaning

**Cases 9, 10, 13, 55.** A translated comment, a missing space after a marker, a
locale prefix, `0o775` against `509`.

**The rule:** normalise before comparing, and normalise on the side where the
difference is a convention rather than a claim.

### H. A deliberate omission taken for a defect

**Cases 17, 20, 29, 31.** Mutexes are not copied, `go.mod` promises nothing about
CI, a deprecated option still works, a removed scenario is not a removed option.

**The rule:** ask what the author meant before calling it a defect.

### I. The tool hides the real thing

**Cases 42 and 59 are the only two in this record where the error worked AGAINST
us. Both belong to the refuter, which is fair: it is the only tool whose job is to
kill findings.**

The refuter killed a real finding in ecologits: the classifier list in
`pyproject.toml` consists of version numbers entirely, and any number nearby
counted as a caveat.

The second case turned up the same evening and cost more. The rule "the
coordinate points at a comment rather than code" is sound for detectors that judge
code. But `doxdrift` reports the line of a `\param` block, a comment **by
construction**. On Boost.Geometry the refuter wiped out all twenty-four findings
and the sweep cheerfully printed "after refutation 0". There was one way to notice
it: doubt your own good result and go read what exactly was removed.

**Why this is recorded apart.** The other fifty-nine cases are lies in our own
favour, which means extra noise. These two are losses. **Hiding a real finding is
worse than letting a false one through**: noise is visible and costs time, a loss
is not visible at all.

Hence the rule for every suppression rule: they have to be **narrow and named**.
This is why the refuter prints a reason for every finding it kills, and why those
reasons deserve an occasional read, especially when a lot was removed at once.

And the second rule, from case 59: **a suppression rule has to know what the
finding is about.** "The coordinate is inside a comment" is an argument against a
finding about code and a hit on target for a finding about documentation. One and
the same observation means opposite things depending on the subject.

### J. The voice taken for somebody else's

**Cases 45, 47, 48.** The lesson harvester reads replies to our pull requests and
has to tell a **maintainer opinion** apart from everything else that settles into
a discussion thread. On the very first live run four lessons out of eight were not
opinions: a checklist template from `github-actions[bot]`, a command to a bot
(`@all-contributors please add @author`, a thank-you in substance), and one reply
arriving from three API endpoints and tripling.

The shared mechanism: **the tool saw text and never asked whose it was and who it
was addressed to.**

### K. The documentation describes something other than what sits beside it

**Cases 50, 51, 56. The species surfaced in two tools and two languages at once.**

Comparing "what is said" against "what is done" rests on a silent assumption: the
comment describes **the declaration it is attached to**. The assumption breaks
three different ways, and all three showed up in one evening.

- **a property.** In networkx `G.edges` is a `cached_property` returning a
  callable view. The docstring describes the call `G.edges(nbunch, data)` while
  the signature of the function itself holds only `self`. Forty-one cases of
  forty-four;
- **an example inside the documentation.** A doctest prints someone else's
  docstring in full, the print comes out unindented, and a `Parameters` section
  inside example output is indistinguishable from a real one;
- **a family of overloads.** asio documents five overloads in one comment while a
  single declaration sits next to it. Twenty-five cases of twenty-five.

The signals differ and the question is one: **is this text really about this
declaration.** The rules came out narrow and checkable: a property shows in the
decorator, an example in the Examples section, a family in a repeated `\param`
name inside one block.

### L. The notation compared instead of the value

**Cases 52, 53, 54, 55.** All four come from comparing default values, and all
four are about the same thing: the tool compared **a string from the docs against
a string from the code** where the meaning was what mattered.

`0.01` truncated at the dot to `0`. `0o775` unrecognised in `509`. `1e-8`
unrecognised in `1e-08`. "default: all nodes in G" compared with `None` as if it
were a value rather than a phrase. And the most frequent one: the code holds a
sentinel `None` while the docs say what it turns into, an idiom of the whole
language.

**The rule:** before declaring two values different, reduce both to values.
Whatever does not reduce is no value, and nothing can be claimed about it.

---

## What the kit is NOT immunised against

An honest list of what we do not know. An empty line here does not mean the area
is clean.

| Area | What has never been checked |
|---|---|
| Languages | Declarations are parsed for Go (a custom parser), Python (`ast`) and C++ (regular expressions). Rust, Java and TypeScript are not parsed at all |
| Documentation formats | numpydoc and Doxygen only. Google style, JSDoc and rustdoc are not read |
| Builds | No tool builds a project. The whole "promise against behaviour" family is out of reach |
| Spec formats | OpenAPI 3 and Swagger 2. GraphQL, AsyncAPI and gRPC-web were never tried |
| Changelogs | The etcd, rclone and nf-core forms are worked out. "Keep a Changelog" with nested lists was never checked |
| Translations | Checked on Hugo with `default_lang_commit`. Docusaurus, Sphinx and MkDocs were never tried |
| Tests | `assertdrift` knows helm-unittest only. pytest, go test and jest are not parsed |
| Locales | The locale prefix is stripped by a two-letter mask. `pt-BR` and `zh-Hans` are checked, the exotic ones are not |
| Scale | The sweep covered 151 clones, of which about thirty are real projects. The rest are Boost libraries |
| Review language | The lesson harvester understands rejection in English only. Chinese and Japanese projects will yield zero lessons and say nothing about it |
| Rejections | Across 61 of our pull requests there is **not one rejection** yet. So the rejection language is tested on invented examples rather than live ones |
| Form of rejection | A maintainer may close a pull request silently or with one word. The tool will not see that: there is no text to take a reason from |

---

## How to add to this file

A new false case takes **five actions, all of them required**:

1. Read it by hand and understand the mechanism rather than the symptom.
2. Add a row to the table with the cost in false findings.
3. Assign it to a species above or start a new one.
4. Pin it down with a test whose text names the live case.
5. If the case exposed something we do not know, add a row to "not immunised".

Skipping the fourth action devalues the first three: a rule with no test lives
until the next edit.
