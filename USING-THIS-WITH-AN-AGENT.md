# Using this kit with a coding agent

The tools here find real defects, and they find them fast. That is exactly what makes them easy to misuse: a checker plus an agent can open twenty pull requests in an afternoon, and twenty pull requests in an afternoon is what gets a maintainer to close all of them without reading.

This happened to us on 2026-08-05. Two people ran the same checker against the same project a day apart, both fixes were correct, and the maintainer closed both — citing the volume, not the content. Nobody did anything wrong except fail to coordinate.

So: the license is MIT and you can do what you like. What follows is not a licence term, it is what we have learned works.

## Say that a tool found it

Not because anyone requires it. Because the alternative is worse.

A maintainer who reads "found with a static checker, verified by hand" knows what they are looking at and can judge it. A maintainer who suspects tooling and was not told feels misled, and that is the one thing that turns a correct patch into a closed one.

One line in the pull request body is enough:

> Found with [driftkit](https://github.com/karpovantonme/driftkit), a checker that compares what a project documents against what it actually does. Each finding was read by hand before the change.

Link it or don't. What matters is that the reader knows.

## Check the project's own rules first

Some projects have an explicit policy on AI-assisted contributions and some do not. Read `CONTRIBUTING`, the pull request template, and `AGENTS.md` if present, before you write anything. If they ask for a disclosure box, fill it in honestly, including the uncomfortable option.

`sitecheck.py` in this kit reads those files and tells you what it found. It is a starting point, not a substitute for reading them.

## Do not send everything the tool reports

The kit lies, in your favour, roughly one time in three. [FALSE-POSITIVES.md](FALSE-POSITIVES.md) is seventy worked cases of exactly how. A finding you have not opened in an editor is not a finding, it is a lead.

The number the tool prints is for you. Never put it in a pull request: on one project we quoted a count from a broken version of a scanner and the library author replied to it.

## Do not land on someone else's project

Before opening anything, look at the project's open and recently closed pull requests for the same kind of change. Thirty seconds:

```console
gh pr list --repo OWNER/NAME --state all --limit 50 --search "docstring OR param OR doxygen"
```

If someone is already there, pick another project or write to them. There is no shortage of projects.

We keep the ones we are working on in [TERRITORY.md](TERRITORY.md) next to this file, so you can see what is taken and add your own. Pull requests to that file are welcome and will be merged without discussion.

## Go narrow

One kind of defect, one project, one pull request. Not "fixed 40 things across the codebase". A maintainer can review a narrow change in five minutes and will; a broad one waits for a free evening that never comes.

## Scientific software deserves extra care

Small communities, maintainers who know each other, and a low tolerance for anything that reads like automated volume. If you go there, go slowly, disclose, and send one thing at a time.
