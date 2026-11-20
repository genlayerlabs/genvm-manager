# Versioning and Release Trains

`.genvm-monorepo-root` is the source of truth, validated against
`support/schemas/genvm-monorepo-root.json` by a pre-commit hook:

1. `version` — the manager's own release version, e.g. `v0.6.0-rc1`
2. `executor-lines` — the shipped executor lines, each an `executors/v<X>.x`
   submodule, mapped to a `status`:

| Status | Behavior | Fixes |
|---|---|---|
| `preview` | may still change; never on a network with real value | any |
| `current` | frozen once activated; owns the manager-global generated files | features, bugs, security |
| `maintained` | frozen | bugs and security, replay-identical |
| `replay-only` | replays finalized history only | selected bugs only |

Exactly 1 line is `current`, which `check-versions lines` enforces. The other
rules are policy: nothing checks where a `preview` line is scheduled. A line's
own `configure` hook reads `replay-only` as `is_support_only`

Executor crates are versioned **independently**: a line's `manifest.json` holds
`executor-version` and no manager script rewrites it. It also holds `fork`, the
fork gating the line, which `install/data/genvm-chainforks.json` must define and
schedule on every network; a new line adds `genvm/executor/<line>` there

A platform release asset bundles every line, and nothing else ships an
executor on its own: the executor repo publishes no releases, so an install root
gets its lines from the manager asset alone

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
| `check-versions bump [minor\|major]` | cut the next manager train, default `minor` |
| `check-versions bump --set 0.6` | explicit major.minor |
| `branch-versions list\|manager` | every executor line, or the manager's train, as bare major.minor |

`bump` sets `version` to `v<new>.0-rc0` and the manager crates to `<new>.0`, and
prints the bare major.minor for callers to build branch names from. It leaves
`executor-lines` alone: a new executor line also needs its submodule and fork

Branch model for feature work: [submodules.md](../committing/submodules.md). Why
the lines exist at all: [executor-lines.md](../../explanation/executor-lines.md)
