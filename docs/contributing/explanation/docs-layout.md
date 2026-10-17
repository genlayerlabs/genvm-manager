# Documentation Layout

Where a new page goes, and why the tree is cut this way. The split follows
[Diátaxis](https://diataxis.fr): 4 kinds of page, each answering a different
question, never mixed on one page

| Kind | Question | Lives in |
|---|---|---|
| Tutorial | How do I get my first change landed? | `docs/contributing/tutorial/` |
| How-To | How do I do this one task? | `docs/contributing/howto/` |
| Reference | What exactly does it do? | `docs/website/src/spec`, `impl-spec` |
| Explanation | Why is it like this? | `docs/contributing/explanation/` |

The cut that matters in practice: **anything normative is reference**. A
statement constraining what an implementation must do belongs in the spec, even
when its reasoning is interesting. Explanation may motivate a rule and link to
it, never restate it — 2 copies drift, and the copy a reader finds first wins

## Why Spec and Impl-Spec Are Separate

GenVM is consensus-critical, so `spec/` is the contract between independent
implementations, readable without this repository open. `impl-spec/` describes
how this codebase happens to satisfy it, and is free to change in any release
that keeps `spec/` true

## Why ADRs Are Not Explanation

An ADR is a dated record of one decision, superseded rather than rewritten; an
explanation page describes the present and is edited freely. So
[`docs/adr/`](../../adr/) stays an archive, off the website, and an accepted
ADR leaves 2 traces: its rule in the spec, its reasoning here

## Audience

`docs/contributing/` is for people changing this repository, `docs/website/`
for people using GenVM. Both are read by humans and AI agents, so
[style.md](../howto/docs/style.md) applies to both
