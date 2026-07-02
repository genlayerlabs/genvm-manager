# Build (debug)

All commands assume the dev shell (`nix develop '.?submodules=1#full'`, usually
via direnv) — it provides `genvm-tool`, `ninja`, `cargo`, `python3`.
First-time clone: [setup.md](../setup.md).

```bash
genvm-tool configure   # generates build/build.ninja + build/info.json (like CMake)
ninja -C build all/bin
```

Outside the dev shell call `support/tools/genvm-tool/genvm-tool configure`.

Re-run `genvm-tool configure` after adding/removing/renaming source files —
symptom if you forget: ninja `missing and no known rule to make it`.

| Ninja target | Builds |
|---|---|
| `all` | everything (incl. runners; x86_64 Linux only) |
| `all/bin` | all Rust binaries |
| `all/data` | runner data via nix |
| `codegen` | code generation (see [genvm-tool.md](../genvm-tool.md)) |

Outputs under `build/out`:

- `bin/genvm-modules` — modules binary
- `executor/<version>/bin/genvm` — one dir per built executor version; the
  version comes from `executors/<line>.x/manifest.json` and is recorded in
  `build/info.json`

Release/nix packages: [release-build.md](../releasing/release-build.md).
Getting or changing runners: [runners.md](runners.md), [modify-runner.md](../extending/modify-runner.md).

## Cargo notes

- Plain `cargo` inside a submodule needs the dev-shell env (lua, pkg-config, …);
  prefer the ninja build.
- In this nix environment, `cargo check` inside an executor's `executor/` dir
  may need `LD_LIBRARY_PATH="$(nix eval --raw nixpkgs#zlib)/lib"` so rustc
  finds libz.
