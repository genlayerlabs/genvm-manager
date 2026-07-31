# Vendored Trees (git-third-party)

Vendored sources such as `executors/v0.3.x/executor/third-party/wasmtime` are
git-ignored and materialized on demand. Why patches rather than a fork:
[vendored-trees.md](../../explanation/vendored-trees.md). What is tracked, per
line, is `.git-third-party/`:

1. `config.json` — per repo: upstream `url`, pinned base `commit`, `patches`
   count, optional `submodules` list
2. `patches/<repo-path>/<n>` — numbered `git format-patch` files, 1-based, mbox
   format, applied on top of the base commit

The tool is `support/tools/git-third-party/git-third-party`, invoked as
`git third-party`, on `PATH` in the dev shell and through `env.sh`. It has no
`--help`; running it bare prints usage

| Command | Effect |
|---|---|
| `add <PATH> <REPO_URL> <COMMIT>` | register a vendored repo (the path must already be git-ignored) and materialize it |
| `update {--all \| <path>...}` | materialize: nested `git init` + fetch of the pinned commit, submodule update, `git am` of the patches |
| `save {--all \| <path>...}` | regenerate the patches from the commits above the base commit, updating the count |

`update` refuses a dirty tree. The result is a real nested git repo: base commit
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
edit `commit` in `config.json` and run `update`; on an `am` conflict fix up the
nested repo's commits and `save`

## Build Integration

1. Local and CI checkouts run `git third-party update --all` **inside each
   executor submodule** — the config resolves from the current git toplevel, so
   running it at the manager root materializes nothing
2. Nix never calls the tool: `support/nix/git-third-party.nix` re-reads
   `config.json` and uses `builtins.fetchGit` plus `pkgs.applyPatches`, ignoring
   `submodules`. So nix sees the committed base plus patches — `save` and commit
   before building any flake package
