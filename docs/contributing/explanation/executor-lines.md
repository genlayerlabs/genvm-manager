# Why Several Executor Lines Ship At Once

A deployed contract is bytes on a chain that nobody can migrate, so the
semantics it was validated against must stay reachable for as long as it does. A
breaking change is therefore a new executor beside the old one, with the
contract naming the one it wants — normatively
[`03-versioning.rst`](../../website/src/spec/01-core-architecture/03-versioning.rst),
decided in [ADR-010](<../../adr/010. versions.md>)

That constraint explains most of the repository's shape

## What It Costs

1. A release ships one binary per active line, so the artifact layout is indexed
   by version rather than flat
2. The lines are branches of one executor repository, checked out as separate
   submodules. They share crate names and versions, so cargo needs a target
   directory per line ([build.md](../howto/building/build.md))
3. Anything manager-global is owned by exactly one line, the first in
   `active-versions` ([versioning.md](../howto/releasing/versioning.md))
4. Every doc, test filter and config key naming a version is a place a new line
   must be added

This is the price of the guarantee, not complexity to be simplified away

## Lines Are Not Peers

`active-versions` is ordered: the first entry is the current line, the rest are
kept so existing work keeps running. `support-only-versions` goes further — still
shipped, no longer built from full sources
