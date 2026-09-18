# Why Submodules Are Worktrees Of One Cache

The submodule paths are not `git submodule` checkouts. One bare cache repo per
remote lives under `<manager .git>/genvm-submodule-cache/`, and every submodule
path is a [git worktree](https://git-scm.com/docs/git-worktree) of it, detached
on the gitlink, all created by `support/scripts/get-all-git.py`
([setup.md](../howto/setup.md))

The two executor lines are branches of the *same* repository
([executor-lines.md](executor-lines.md)), so a plain `git submodule update`
clones it twice, and once more for every git worktree of the manager. Sharing
the objects with `--reference` instead only trades that for a fragility: the
borrowing checkouts hold no reference the cache can see, so pruning or deleting
the cache corrupts them silently. A worktree's HEAD *is* a ref in that
repository — the objects it needs cannot be collected out from under it, and
the shared store is the only copy that exists

A second git worktree of the manager makes that worse rather than better: its
submodule gitdirs are private to it (`.git/worktrees/<id>/modules/<path>`), so
nothing at all is shared with the checkout it was branched from. Under the cache
every manager checkout draws its executor trees from the same objects

Converting an existing checkout is therefore non-destructive: only its `.git`
file is replaced, every ref it had is mirrored into the cache under
`refs/genvm/converted/<manager>/<path>/` — branches, tags, notes and the stash —
and the files, including the ignored third-party trees, are never touched

## What It Costs

`git submodule update` is no longer the way to materialize a checkout — it
would clone over the worktree — so a gitlink bump means re-running the script,
and the gitdir a converted checkout used (`.git/modules/<path>`, or
`.git/worktrees/<id>/modules/<path>` in a linked worktree) is left behind: its
refs were copied out, its reflogs were not, so it is yours to keep or delete.
Both executor lines also share one ref store, which is why an
executor feature branch is namespaced by line (`pr/<line>/…`) — that convention
predates this and is what makes the sharing safe
