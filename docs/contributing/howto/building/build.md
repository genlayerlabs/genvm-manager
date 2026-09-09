# Debug Build

Everything assumes the dev shell, which provides `genvm-tool`, `ninja`, `cargo`
and `python3`. First clone: [setup.md](../setup.md)

```bash
genvm-tool configure   # writes build/build.ninja and build/info.json, like CMake
ninja -C build all/bin
```

Re-run `configure` after adding, removing or renaming a source file; forgetting
shows up as ninja's `missing and no known rule to make it`

| Ninja target | Builds |
|---|---|
| `all` | `all/bin` + `all/data` |
| `all/bin` | every Rust binary: the manager and each executor line |
| `all/manager` | the manager binary only |
| `all/executor/<line>` | one executor line |
| `all/data` | runner data via nix |
| `all/runners` | the runners themselves via nix, x86_64 Linux only |
| `codegen` | generated sources ([genvm-tool.md](../genvm-tool.md)) |

Outputs land in `build/out`: `bin/genvm-modules`, `bin/genvm-manager`,
`bin/genvm-post-install`, and `executor/<version>/bin/genvm` per built line,
where the version comes from `executors/<line>.x/manifest.json` and is recorded
in `build/info.json`

Release packages: [release-build.md](../releasing/release-build.md). Runners:
[runners.md](runners.md)

## Cargo Notes

1. Prefer the ninja build — plain `cargo` in a submodule needs the dev-shell
   environment (lua, pkg-config, …)
2. `cargo check` in an executor's `executor/` may need
   `LD_LIBRARY_PATH="$(nix eval --raw nixpkgs#zlib)/lib"` so rustc finds libz
3. Each line has its own target directory
   (`build/ya-build/rust-target/<line>`, in `build/info.json` under
   `rust_target_dirs`); crates outside a line use the parent. Lines ship crates
   with identical names and versions, and cargo's artifact hash ignores where a
   package came from, so a shared directory would let them overwrite each other
4. Build and tests pass `--target <host triple>` (`rust_target` in
   `build/info.json`). Without it cargo builds a second, unshared unit graph in
   the same directory and recompiles everything
