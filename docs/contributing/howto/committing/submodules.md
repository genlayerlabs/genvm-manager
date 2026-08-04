# Submodules

## Topology

1. **Manager**, the umbrella — this repo: `flake.nix`, `implementation/`,
   `runners/`, `support/`, `install/`
2. **Executor lines** — `executors/v0.2.x`, `executors/v0.3.x`: checkouts of
   the **same** repo, `genlayerlabs/genvm-executor`, on different branches.
   Each line's `manifest.json` owns its `executor-version`
3. **`libs/unhardcoded-engine`** — the LLM policy engine used by
   `implementation/`; the build fails loudly when it is missing

Active lines and the manager version live in `.genvm-monorepo-root`
([versioning.md](../releasing/versioning.md)); `genvm-tool git` and the
`commit-hooks` pipeline model the manager and the active lines only. Branches:
manager `feat/<name>` on `v<X>-dev`, each line `pr/<line>/feat/<name>`

## Materializing A Checkout

`python3 support/scripts/get-all-git.py` — never `git submodule update`, which
would clone over the worktree each path actually is
([shared-submodule-cache.md](../../explanation/shared-submodule-cache.md)). Run
it after any gitlink bump; it fetches what is missing and parks each checkout on
the gitlink, exactly as `git submodule update` did. `origin` inside an executor
is the executor repo, so `git fetch` / `git push` work as usual

An older checkout is converted on the spot, keeping every file and uncommitted
edit, and its branch when that branch is already at the gitlink. It refuses when
something is staged there, and when the path holds a standalone clone rather
than a submodule checkout — move that aside and re-run

## The Critical Nix Flag

Every manager flake ref needs `?submodules=1`, as `.envrc` does. Flake packages
see **committed** submodule content only, so commit inside a submodule before
building packages. An executor's own flake is a standalone dev shell and takes
no such flag

## Committing Across Repos

The manager pins each submodule by a gitlink, so the order matters:

1. Commit **inside** the submodule
2. `git add executors/<line>.x` in the manager
3. Commit the manager: code and gitlink bumps together

Verify with `git ls-tree HEAD executors/v0.3.x` against
`git -C executors/v0.3.x rev-parse HEAD`. To reshape unpushed history:
`git reset --mixed <base>`, re-stage per commit, re-bump the gitlink

## Pre-Commit Hooks

Each repo declares its hooks in its `flake.nix`
([git-hooks.nix](https://github.com/cachix/git-hooks.nix)); entering a repo's
dev shell installs them, so `git commit` runs the hooks of the repo you commit
in. Never `git commit --no-verify`, not even for a pure gitlink bump

1. Manager: `nix develop '.?submodules=1#full' --command pre-commit run --all-files`
2. Everything, as CI does: `./support/ci/run.sh pipeline commit-hooks`

Executor crates reach the manager's shared `crates/` by relative paths, so
executor hooks must run against the **nested working tree** — hence the in-tree
pipeline rather than `nix flake check`

## Pushing

Push **submodules before the manager**, so the gitlinks resolve.
`genvm-tool git check-for-push` reports readiness per repo (`--offline`
compares against the last fetch) and prints one aggregated
`suggested_push_command`, `none` while anything is not ready

```bash
git -C executors/v0.3.x push origin pr/v0.3/feat/<name>
git push origin feat/<name>
```

A rebased manager branch needs `--force-with-lease`; executor feature branches
are normally ahead-only. Runner hash hygiene first: [runners.md](runners.md)

## Why There Are Two Runner Trees

Forward-rolling lines share the top-level `runners/<id>/<aa>/<rest>.tar` tree,
named by Crockford base32 of `sha256(tar)`; frozen v0.2.x keeps
`executor/<version>/legacy-runners/` with Nix base32 hashes. `flake.nix` splits
the runner list per line, and `genvm check` verifies each with its own scheme
