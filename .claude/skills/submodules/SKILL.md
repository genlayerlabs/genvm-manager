---
name: submodules
description: How the GenVM multi-repo (manager umbrella + executor submodules) fits together and how to build, commit, run hooks, and push across all of them. Use when committing/pushing changes that touch a submodule, bumping gitlinks, building via nix, rebasing the manager, or debugging the cross-repo pre-commit hook.
---

# Working with the GenVM multi-git submodule setup

## Topology

- **Manager (umbrella)** — this repo (`git@github.com:genlayerlabs/genvm-manager`). Holds `flake.nix`, `implementation/` (the `genvm-modules` manager binary), `runners/`, `support/tools/genvm-tool`, `install/`, and the build glue.
- **Executor submodules** — `executors/v0.2.x` and `executors/v0.3.x`, both checkouts of the **same** repo `git@github.com:genlayerlabs/genvm-executor` on different branches (see `.gitmodules`). Each `executors/<line>.x/manifest.json` is the source of truth for that line's `executor-version` (e.g. `v0.2.17`, `v0.3.0-rc7`).
- `.genvm-monorepo-root` lists the active lines and the manager's own release `version`.

### Branch model

Feature work uses matching branches across repos:
- manager: `feat/<name>` (base branch is usually `v0.6-dev`).
- each executor submodule: `pr/<line>/feat/<name>` (e.g. `pr/v0.2/feat/add-v0.2`).

`genvm-tool git check-for-push` reports, per repo, whether the submodule is on the same-named branch as the manager and whether its current HEAD is committed in the manager's gitlink.

## Building (nix + submodules — the critical flag)

The flake reads submodule content, so **every nix invocation must pass `?submodules=1`**:

```bash
nix develop '.?submodules=1#full' --command bash .claude/skills/build/scripts/run-ninja.sh -C build all/bin
nix build '.?submodules=1#genvm' --out-link /tmp/genvm-built
```

Without `?submodules=1` you get: `Path 'executors/v0.3.x' ... is not tracked by Git` while evaluating `packages`.

- After **adding/removing/renaming** a source file, regenerate the ninja file list first: `nix develop '.?submodules=1#full' --command genvm-tool configure` (then rebuild). Symptom if you forget: `missing and no known rule to make it`.
- Output: `build/out/bin/genvm-modules`, `build/out/executor/<version>/bin/genvm`.

### Nix can't see uncommitted submodule content

`nix build`/`nix develop` of the **flake packages** only sees **committed** submodule files. Untracked or dirty files in a submodule make the flake fail to evaluate. So: **commit inside the submodule first**, then build the flake packages. (Plain `cargo` inside a submodule can't be used standalone — it needs the dev-shell env, e.g. lua/pkg-config; use the ninja build.)

## Committing across repos

Order matters because the manager tracks each submodule by a **gitlink** (a pinned commit):

1. Make and commit changes **inside the submodule(s)** (`cd executors/<line>.x && git add … && git commit`).
2. In the manager, stage the submodule pointer to bump the gitlink: `git add executors/<line>.x`.
3. Commit the manager change (manager code + gitlink bumps together).

Keep each commit's files coherent (don't let a submodule's linter reformat leak a file into an unrelated commit). Follow `/commit-style` for messages. To reshape unpushed history, `git reset --mixed <base>` and re-stage per commit; then re-bump the manager gitlink to the new submodule HEAD.

Verify consistency any time with:
```bash
git ls-tree HEAD executors/v0.2.x executors/v0.3.x     # manager's pinned gitlinks
git -C executors/v0.2.x rev-parse HEAD                 # actual submodule HEAD (must match)
```

## The cross-repo pre-commit hook

`git commit` triggers `genvm-tool hook run` (installed stub), which fans out linters/formatters to the manager **and** its submodules. Notes:

- Run it manually over everything: `genvm-tool hook run --all-files` (default `--fix` rewrites in place; `--check` only verifies — CI uses `--check`).
- `--no-verify` bypasses the hook for a commit (useful for gitlink-only bumps, or committing already-formatted content).
- After changing genvm-tool itself, rebuild + reinstall the stubs: `genvm-tool hook install` (builds `support/tools/genvm-tool/default.nix` into `.direnv/genvm-git-helper/` and rewrites the stubs).
- The stub clears `PYTHONPATH`/`NIX_PYTHONPATH` and `genvm-tool hook run` strips inherited `GIT_*` vars, so hooks run their own deployed code and don't crash on submodule `git` calls. If you hit a hook crash referencing an old `/nix/store/...genvm-tool`, your dev shell's `PYTHONPATH` is stale — refresh the dev env (or re-run `genvm-tool hook install` from a freshly built tool with `PYTHONPATH` unset).

## Pushing

Check first: `genvm-tool git check-for-push` (add `--offline` to compare against the last fetch instead of querying the remote).

Push **submodules before the manager** (so the gitlinks resolve on the remote):

```bash
git -C executors/v0.2.x push origin pr/v0.2/feat/<name>
git -C executors/v0.3.x push origin pr/v0.3/feat/<name>
git push origin feat/<name>
```

- Executor feature branches are normally ahead-only → plain fast-forward pushes.
- If the manager was **rebased** onto its base (e.g. `origin/v0.6-dev`), it diverges (`ahead N, behind M`) and needs `git push --force-with-lease origin feat/<name>`.

## Runners layout (why there are two runner trees)

- Forward-rolling lines (v0.3.x) share the top-level `runners/<id>/<aa>/<rest>.tar` tree; hashes are Crockford base32 of `sha256(tar)`.
- The **frozen v0.2.x** line keeps its runners privately under `executor/<version>/legacy-runners/`; its registry hashes are **Nix base32** of `sha256(tar)` — a different scheme, which is exactly why the trees are kept separate. The packaging (`flake.nix`) splits the accumulated runner list by line and lays each set into the right place; `post-install` downloads into the matching dir. The executor's `genvm check` verifies each line with its own scheme.
