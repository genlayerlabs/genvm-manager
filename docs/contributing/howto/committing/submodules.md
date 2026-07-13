# Submodules (multi-repo workflow)

## Topology

- **Manager (umbrella)** — this repo (`genlayerlabs/genvm-manager`): `flake.nix`,
  `implementation/`, `runners/`, `support/tools/genvm-tool`, `install/`.
- **Executor submodules** — `executors/v0.2.x`, `executors/v0.3.x`: checkouts of
  the **same** repo (`genlayerlabs/genvm-executor`) on different branches (see
  `.gitmodules`). Each line's `manifest.json` owns its `executor-version`.
- `.genvm-monorepo-root` lists the active lines and the manager's `version` —
  see [versioning.md](../releasing/versioning.md).

Branch model: manager `feat/<name>` (base usually `v<X>-dev`); each executor
submodule `pr/<line>/feat/<name>`.

## The critical nix flag

Every flake ref needs `?submodules=1` (`nix develop '.?submodules=1#full'`,
`nix build '.?submodules=1#genvm'`). Flake packages only see **committed**
submodule files — commit inside the submodule before building packages.

## Committing across repos (order matters)

The manager pins each submodule by a **gitlink** (a pinned commit):

1. Commit **inside** the submodule: `cd executors/<line>.x && git add … && git commit`.
2. In the manager, bump the gitlink: `git add executors/<line>.x`.
3. Commit the manager (code + gitlink bumps together).

Verify consistency:

```bash
git ls-tree HEAD executors/v0.2.x executors/v0.3.x   # pinned gitlinks
git -C executors/v0.3.x rev-parse HEAD               # must match
```

To reshape unpushed history: `git reset --mixed <base>`, re-stage per commit,
re-bump the gitlink to the new submodule HEAD.

## Pre-commit hook (cross-repo)

`git commit` triggers `genvm-tool hook run`, which fans linters/formatters out
to the manager **and** submodules.

- Everything at once: `genvm-tool hook run --all-files` (default `--fix`
  rewrites; `--check` only verifies — what CI uses).
- `git commit --no-verify` skips the hook — fine for pure gitlink bumps or
  already-formatted content.
- After changing genvm-tool itself: `genvm-tool hook install` rebuilds the tool
  and rewrites the hook stubs. A hook crash referencing an old
  `/nix/store/...genvm-tool` means a stale dev-shell `PYTHONPATH` — refresh the
  dev env or re-run `hook install` with `PYTHONPATH` unset.

## Pushing

Check first: `genvm-tool git check-for-push` (`--offline` compares against the
last fetch). Push **submodules before the manager** so gitlinks resolve:

```bash
git -C executors/v0.2.x push origin pr/v0.2/feat/<name>
git -C executors/v0.3.x push origin pr/v0.3/feat/<name>
git push origin feat/<name>
```

After rebasing the manager onto its base (e.g. `origin/v0.6-dev`), it diverges:
use `git push --force-with-lease origin feat/<name>`. Executor feature branches
are normally ahead-only (plain fast-forward push).

For runner hash hygiene before committing (dev-mode, `hash-updater.py`), see
[runners.md](runners.md).

## Runner trees (why there are two)

Forward-rolling lines (v0.3.x) share the top-level `runners/<id>/<aa>/<rest>.tar`
tree (Crockford base32 of `sha256(tar)`). Frozen v0.2.x keeps runners under
`executor/<version>/legacy-runners/` with **Nix base32** hashes — a different
scheme, hence the separate trees. `flake.nix` packaging splits the runner list
per line; `genvm check` verifies each with its own scheme.
