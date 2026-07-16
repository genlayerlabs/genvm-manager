# First-time setup

1. Get the executor submodules and vendored third-party trees **before** entering
   the dev shell (the nix flake reads them):

   ```bash
   python3 support/scripts/get-all-git.py
   ```

   The default options are sufficient for normal setup: the command resolves
   duplicate submodule remotes, fetches each unique remote once into a local
   cache, initializes submodules from that cache, and materializes all
   third-party trees.

2. Enter the dev shell:

   ```bash
   nix develop '.?submodules=1#full'
   ```

   `?submodules=1` is required on **every** flake ref — without it:
   `Path 'executors/v0.3.x' … is not tracked by Git`. Usually auto-loaded via
   direnv (`.envrc` is `use flake '.?submodules=1#full'`).

3. `source env.sh` — adds `support/tools/git-third-party` to PATH and sources
   `.env` if present. Only needed outside the dev shell for manual
   `git third-party` use.

See [git-third-party.md](committing/git-third-party.md) for how vendoring works.

Next: [build.md](building/build.md).
