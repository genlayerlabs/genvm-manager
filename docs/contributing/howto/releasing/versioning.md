# Versioning and Release Trains

`.genvm-monorepo-root` is the source of truth:

1. `version` — the manager's own release version, e.g. `v0.6.0-rc1`
2. `active-versions` — the live executor lines, each an `executors/v<X>.x`
   submodule. The first is the primary line, which owns the manager-global
   generated files
3. `support-only-versions` — lines still shipped but no longer built from full
   sources

Executor crates are versioned **independently**: a line's `manifest.json` holds
`executor-version` and no manager script rewrites it

Manager branches are named after the *manager* train, not an executor line:
`v<X>` is the release branch, `v<X>-dev` the integration branch. Here `<X>` is
major.minor, for example `v0.6`

On each manager branch push, `.gitmodules` declares the executor release
branches to update. A manager dev push derives `<line>-dev` from each declared
`<line>.x`; all updates are branch creation or fast-forward only

## Tools

Both are `support/ci/tools/versions.py`, run through `./support/ci/run.sh tool`.
`MONOREPO_ROOT=<path>` points them at a different `.genvm-monorepo-root`, e.g.
one extracted from `origin/main`

| Command | Effect |
|---|---|
| `check-versions sync` (default) | pre-commit fixer: rewrite `implementation/Cargo.toml` major.minor to match `version`, keeping the patch; non-zero exit if it changed anything |
| `check-versions bump [minor\|major]` | cut the next train, default `minor` |
| `check-versions bump --set 0.6` | explicit major.minor |
| `branch-versions list\|manager` | the active executor lines, or the manager's train, as bare major.minor |

`bump` updates `version`, appends the new train to `active-versions`, sets the
manager crates to `<new>.0`, and prints the bare major.minor for callers to
build branch names from

Branch model for feature work: [submodules.md](../committing/submodules.md). Why
the lines exist at all: [executor-lines.md](../../explanation/executor-lines.md)
