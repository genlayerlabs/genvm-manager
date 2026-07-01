---
name: build
description: Builds the GenVM project. Use after making code changes to compile Rust binaries.
---

To build the GenVM project:

> **All commands here assume you're inside the project dev shell** —
> `nix develop '.?submodules=1#full'`, usually provided automatically by direnv,
> so `genvm-tool`, `ninja`, `cargo`, `python3` are on PATH. The `?submodules=1`
> matters: the flake reads the executor submodules and fails without it, and only
> sees **committed** submodule files — so commit inside a submodule before
> building its packages (`nix build '.?submodules=1#…'`). See `/submodules`.

## Reconfiguring

If ninja fails with `missing and no known rule to make it` (e.g., after adding/removing/renaming source files), regenerate `build/build.ninja` with the current file list:

```bash
genvm-tool configure
```

## Building

**Build all Rust binaries:**
```bash
bash .claude/skills/build/scripts/run-ninja.sh -C build all/bin
```

This runs ninja silently and only shows output on failure (to save tokens).

**Available ninja targets:**

| Target | Description |
|--------|-------------|
| `all` | Build everything |
| `all/bin` | Build all Rust binaries |
| `all/data` | Build data about runners using Nix |
| `codegen` | Run code generation |

**Output locations:**
- `out/bin/genvm-modules` - modules binary
- `out/executor/<version>/bin/genvm` - executor binary (one dir per built version, e.g. `out/executor/v0.3.0-rc7/`; the concrete version comes from `executors/<line>.x/manifest.json` and is recorded in `build/info.json`)

## Runners

Runners can only be built on x86_64 using the `all` target. On other platforms, download them instead.

**Download runners:**
```bash
python3 build/out/bin/post-install.py --create-venv false --default-step false --runners-download true --error-on-missing-executor false
```

### Runner Development Workflow

To develop/modify a runner (e.g., cloudpickle):

1. **Enable dev mode:**
   Set `runners/support/versions/dev-mode.nix` to `true`

2. **Set hash to "test":**
   In `runners/support/versions/current.nix`, set the runner's hash to `"test"`

3. **Make your modifications and run tests**
   With dev-mode enabled and hash set to "test", you can build and run tests.

4. **Disable dev mode:**
   Set `runners/support/versions/dev-mode.nix` back to `false`. The build will now tell you to set hashes to `null`.

5. **Set hashes to null and build:**
   Set the runner's hash (and dependent runners' hashes) to `null`, then build:
   ```bash
   ninja -C build all
   ```
   The build will fail with a hash mismatch showing the new hash.

6. **Update hashes:**
   Copy the new hash from the error message back into `hashes.nix`. Repeat for dependent runners.

7. **Rebuild to verify:**
   Run the build again to confirm all hashes are correct.

## See also

- `/submodules` — the multi-repo layout, why `?submodules=1` is required, and how to commit/push across the manager + executor submodules.
- `/test` — running tests after a build.
- `/macos` — do **not** build runners natively on macOS; use a remote Linux builder or download them.
