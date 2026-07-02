# git-third-party (vendored trees)

Vendored third-party sources (e.g. `executors/v0.3.x/executor/third-party/wasmtime`,
`.../wasm-tools`) are **not** tracked by the executor repo — they are
git-ignored (`executor/.gitignore: third-party`) and materialized on demand.
What *is* tracked, per executor line, is `.git-third-party/`:

- `.git-third-party/config.json` — per repo: upstream `url`, pinned base
  `commit`, `patches` count, optional `submodules` list (absent = update all
  submodules, `[]` = none, list = only those).
- `.git-third-party/patches/<repo-path>/<n>` — numbered `git format-patch`
  files (1-based, mbox format) applied on top of the base commit.

The tool is `support/tools/git-third-party/git-third-party` (single Python
script; on PATH via `env.sh` and the dev shell; invoked as `git third-party`).
It has **no `--help`** — running with no subcommand prints usage. Subcommands:

| Command | Effect |
|---|---|
| `git third-party add <PATH> <REPO_URL> <COMMIT>` | register a new vendored repo (path must already be git-ignored) and materialize it |
| `git third-party update {--all \| <path>...}` | materialize: nested `git init`+fetch of the pinned commit, submodule update, then `git am` the patches |
| `git third-party save {--all \| <path>...}` | regenerate patch files from commits above the base commit; updates the count in `config.json` |

`update` refuses to run on a dirty vendored tree. The materialized tree is a
real nested git repo whose history is `base commit` + one commit per patch.

## Editing a vendored tree (e.g. a wasmtime patch)

1. Edit files in `executors/<line>.x/executor/third-party/wasmtime` and
   **commit inside that nested repo** (normal `git commit`; patches are derived
   from history above the base commit).
2. `git third-party save executor/third-party/wasmtime` (path relative to cwd) —
   rewrites `.git-third-party/patches/...` deterministically
   (`format-patch --zero-commit --no-signature --numbered-files`).
3. In the executor repo: `git add .git-third-party/`, commit, push.
4. In the manager: bump the gitlink ([submodules.md](submodules.md)).

The nested repo itself is never pushed — only the patch files persist.

To bump the pinned upstream version: edit `commit` in `config.json`, then
`git third-party update <path>` (patches are re-applied with `git am`; resolve
conflicts by fixing up the nested repo's commits and running `save`).

## Build integration

- Local/CI checkouts run `git third-party update --all` **inside each executor
  submodule** — the config is resolved from the current git toplevel, so running
  it once at the manager root materializes nothing (CI:
  `.github/actions/get-src/action.yaml`; local: [setup.md](../setup.md)).
- Nix does **not** call the tool: `support/nix/git-third-party.nix` re-reads
  `config.json` and uses `builtins.fetchGit` + `pkgs.applyPatches`, mounted into
  the source layout by `support/default.nix`. So nix builds see exactly the
  committed base commit + patches — another reason to `save` and commit before
  building flake packages.
