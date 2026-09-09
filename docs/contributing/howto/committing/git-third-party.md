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

## Advisory Monitoring

`branch / wasmtime watch` checks every vendored source of every active line
against [OSV](https://osv.dev) daily and keeps one `wasmtime-maintenance` issue
in sync with the result. It gates nothing and writes no branch. Run it by hand
with `./support/ci/run.sh tool wasmtime-watch --dry-run`

Two things are queried per line, because neither covers the other:

1. each repo in `manifest.json`, by its pinned upstream commit
2. each crate that comes out of one, by its locked version — a vendored tree is
   dozens of crates (`cranelift-codegen`, `wasmparser`, `wiggle`), and that is
   where most advisories against this stack are actually published

A crate counts as vendored when `executor/Cargo.lock` gives it no `source` and
no tracked `Cargo.toml` in the executor repo declares it. Registry crates are
excluded because they are covered upstream; first-party crates, because they are
ours. A first-party crate with no committed manifest falls through and is
queried too — a dismissible false positive, chosen over a silent gap

OSV knows nothing about the features GenVM disables or what the patch series
changes, so a hit is a finding to rule on, not a proven exposure

The sweep edits its issue in place and never closes or reopens one: closing it
is the ruling that its findings are handled. The advisory ids that issue covered
are recorded in its body marker, so later sweeps stay quiet until an id outside
that set appears, at which point a fresh issue supersedes it.
`WASMTIME_REBASE_OWNER` (an Actions variable) is the login the issue is assigned
to at creation; sweeps never reassign it
