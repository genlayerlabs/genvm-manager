# First-time setup

1. Init the executor submodules **before** entering the dev shell (the nix flake reads them):

   ```bash
   git submodule update --init --recursive --depth 1
   ```

2. Enter the dev shell:

   ```bash
   nix develop '.?submodules=1#full'
   ```

   `?submodules=1` is required on **every** flake ref — without it:
   `Path 'executors/v0.3.x' … is not tracked by Git`. Usually auto-loaded via
   direnv (`.envrc` is `use flake '.?submodules=1#full'`).

3. `source env.sh` — adds `support/tools/git-third-party` to PATH and sources
   `.env` if present. Only needed outside the dev shell.

4. Materialize the vendored third-party trees (wasmtime, wasm-tools):

   ```bash
   git third-party update --all
   ```

   See [git-third-party.md](committing/git-third-party.md) for how vendoring works.

Next: [build.md](building/build.md).
