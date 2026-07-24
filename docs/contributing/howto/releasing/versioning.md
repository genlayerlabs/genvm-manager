# Versioning and release trains

`.genvm-monorepo-root` (repo root, JSON) is the source of truth:

- `version` — the manager's own release version (e.g. `v0.6.0-rc0`).
- `active-versions` — the live release trains (e.g. `["v0.3", "v0.2"]`); each
  train maps to an `executors/v<X>.x` submodule line.

Executor crates are versioned **independently** in their submodules (each
line's `manifest.json` holds `executor-version`); manager scripts never rewrite
them.

## Scripts

`support/ci/check-versions.py` — keeps manager-owned crate versions
(`implementation/Cargo.toml`) in sync with `version`:

| Command | Effect |
|---|---|
| `check-versions.py sync` (default) | pre-commit fixer: rewrite crate `[package]` major.minor to match `version` (patch kept); non-zero exit if it changed anything |
| `check-versions.py bump minor` | cut next train, e.g. 0.3 → 0.4 (default level) |
| `check-versions.py bump major` | 0.3 → 1.0 |
| `check-versions.py bump --set 0.6` | explicit major.minor |

`bump` updates `version`, appends the new train to `active-versions`, sets
manager crates to `<new>.0`, and prints the bare major.minor (callers build
branch names from it).

`support/ci/branch-versions.py list|manager` — `list` prints the active
executor lines from `active-versions`; `manager` prints the manager's own train
from `version` (both as bare major.minor). The manager dev/release branches are
named after `manager`, NOT the executor lines. Branch naming per train `<X>`:

- `v<X>.x` — release branch (release-ready)
- `v<X>-dev` — integration branch

`MONOREPO_ROOT=<path>` points it at a different `.genvm-monorepo-root` (e.g.
extracted from `origin/main`).

See also: [submodules.md](../committing/submodules.md) for the branch model of feature work.
