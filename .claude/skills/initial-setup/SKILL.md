---
name: initial-setup
description: Sets up the development environment for GenVM repository. Use when setting up the repo for the first time or when dependencies need to be refreshed.
---

To set up the GenVM development environment:

1. **Initialize the executor submodules first** (the nix flake reads them, so they must be checked out before you enter the dev shell):
   ```bash
   git submodule update --init --recursive --depth 1
   ```

2. **Enter the Nix flake environment:**
   ```bash
   nix develop '.?submodules=1#full'
   ```
   `?submodules=1` is **required** — without it the flake fails to evaluate
   (`Path 'executors/v0.3.x' … is not tracked by Git`). This is usually wired up
   via direnv so every other skill's commands "just work". See `/submodules`.

3. **Source environment variables:**
   ```bash
   source env.sh
   ```
   This adds `support/tools/git-third-party` to PATH and sources `.env` if it exists.

4. **Update third-party dependencies:**
   ```bash
   ./support/tools/git-third-party/git-third-party update --all
   ```
   This updates wasmtime, wasm-tools, and applies GenVM-specific patches. (After
   step 3, `git-third-party` is also on PATH.)

The repository will be ready for development with all dependencies properly configured.

## See also

- `/submodules` — the multi-repo layout, `?submodules=1`, and cross-repo commit/push flow.
- `/build` — building the Rust binaries once set up.
