# First-Time Setup

1. Materialize the submodules and the vendored third-party trees **before**
   entering the dev shell — the nix flake reads them:

   ```bash
   python3 support/scripts/get-all-git.py
   ```

   The defaults fetch every unique submodule remote once into a local cache,
   initialize the submodules from it, and materialize all third-party trees
2. Enter the dev shell — normally direnv does this, `.envrc` is
   `use flake '.?submodules=1#full'`:

   ```bash
   nix develop '.?submodules=1#full'
   ```

   `?submodules=1` is mandatory on every manager flake ref; without it nix
   fails with `Path 'executors/v0.3.x' … is not tracked by Git`
3. Only outside the dev shell, and only for manual `git third-party` calls:
   `source env.sh` puts `support/tools/git-third-party` on `PATH` and sources
   `.env` if present

Next: [build.md](building/build.md). Vendoring mechanics:
[git-third-party.md](committing/git-third-party.md)
