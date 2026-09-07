# Vendored Trees (git-third-party)

Vendored sources such as `executors/v0.3.x/executor/third-party/wasmtime` are
git-ignored and materialized on demand. Why patches rather than a fork:
[vendored-trees.md](../../explanation/vendored-trees.md). What is tracked, per
line, is `.git-third-party/`:

1. `manifest.json` — per repo: upstream `url`, pinned base `commit`, the
   ordered `patches` list, optional `submodules` list
2. `patches/<repo-path>/<name>` — `git format-patch` files in mbox format,
   applied on top of the base commit in the order the manifest lists. `<name>`
   is a digest of the patch bytes, so inserting a patch renames nothing else
3. `.gitattributes`, `.gitignore` — written by the tool. The first keeps the
   enclosing repo from normalizing bytes the names are derived from

The tool comes from the `git-third-party` flake input, on `PATH` in the dev
shell and through `env.sh`, and is invoked as `git third-party`. It has no
`--help`; running it bare prints usage. Bump it with
`nix flake update git-third-party`

| Command | Effect |
|---|---|
| `add <PATH> <REPO_URL> <COMMIT>` | register a vendored repo (the path must already be git-ignored) and materialize it |
| `update {--all \| <path>...}` | materialize: nested `git init` + fetch of the pinned commit, submodule update, `git am` of the patches |
| `save {--all \| <path>...}` | regenerate the patches from the commits above the base commit, rewriting the manifest's list |

`update` refuses a dirty tree — untracked files and dirty submodules count —
and refuses to discard commits in a managed checkout that `save` has not
captured. `git am` runs with `--keep-cr --whitespace=nowarn --no-3way`, so
local git config cannot rewrite what a patch applies, and a patch that only
landed by 3-way fallback now fails instead. Materialized checkouts get their
push URL disabled. The result is a real nested git repo: base commit
plus one commit per patch. It always runs
`git submodule update --init --recursive --depth 1` first, so `submodules: []`
only skips the second pass and a list only adds a targeted one

## Editing a Vendored Tree

1. Edit, and **commit inside the nested repo** — patches come from the history
   above the base commit
2. `git third-party save executor/third-party/wasmtime`, path relative to the
   current directory
3. In the executor repo: `git add .git-third-party/`, commit, push
4. Bump the gitlink in the manager ([submodules.md](submodules.md))

The nested repo is never pushed, only the patches persist. To bump upstream,
edit `commit` in `manifest.json` and run `update`; on an `am` conflict fix up
the nested repo's commits and `save`

## Build Integration

1. Local and CI checkouts run `git third-party update --all` **inside each
   executor submodule** — the config resolves from the current git toplevel, so
   running it at the manager root materializes nothing
2. Nix never calls the tool: `support/nix/git-third-party.nix` re-reads
   `manifest.json` and uses `builtins.fetchGit` plus `pkgs.applyPatches`, ignoring
   `submodules`. So nix sees the committed base plus patches — `save` and commit
   before building any flake package
