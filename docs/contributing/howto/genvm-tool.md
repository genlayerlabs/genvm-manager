# genvm-tool

Lives at `support/tools/genvm-tool` (on PATH as `genvm-tool` in the dev shell;
outside it, call `support/tools/genvm-tool/genvm-tool`).

## Commands

| Command | Purpose |
|---|---|
| `configure` | generate `build/build.ninja` + `build/info.json` — [build.md](building/build.md) |
| `test` | run the test suite — [testing/README.md](testing/README.md) |
| `codegen` | generate language bindings from data JSON (below) |
| `build-manifest` | generate the manager's `data/manifest.yaml` |
| `git ls` / `list-repo` / `create-branches` / `check-for-push` | git helpers across manager + submodules — [submodules.md](committing/submodules.md) |

Command registration: `genvm_tool/__main__.py` (`TOPLEVEL`, `GROUPS`); each
command module has `NAME`/`HELP` — look there for details.

## codegen

```bash
genvm-tool codegen --lang {rust,python,rst,go} -i <data.json> -o <out>
```

(`--go-package` selects the go package name, default `genvm`.)

Codegen data lives per executor line at
`executors/v<X>.x/executor/codegen/data/{public-abi.json,host-fns.json}`.
After editing the JSON, regenerate via the build (`genvm-tool configure` then
`ninja -C build codegen`) or run `genvm-tool codegen` by hand per the tables:

Per-line generated files (registered by `register_standard_codegen` in
`tests/runner/genvm_tool_plugins/ninja.py`, paths relative to the line root):

| Output | Input | Lang |
|---|---|---|
| `executor/crates/sdk-rs/src/abi/consts.rs` | `public-abi.json` | rust |
| `runners/genlayer-py-std/src/genlayer/vm/public_abi.py` | `public-abi.json` | python |
| `executor/crates/common/src/host_fns.rs` | `host-fns.json` | rust |

Manager-global generated files (from the **primary** line's data, wired in
`genvm_tool/cmd_configure.py`) — these go stale when a line's codegen JSON
changes and are refreshed by the same configure/ninja codegen step:

| Output | Input | Lang |
|---|---|---|
| `tests/runner/origin/host_fns.py` | `host-fns.json` | python |
| `tests/runner/origin/public_abi.py` | `public-abi.json` | python |
| `docs/website/src/spec/appendix/constants.rst` | `public-abi.json` | rst |

## Plugins and test definitions

Plugins (`tests/runner/genvm_tool_plugins/`) are **importable libraries** —
reusable build/test helpers, no side effects on import. Test *definitions* are
kept out of the plugin package: each lives in `tests/system/<name>/test.py`
exposing `collect(ctx, **kwargs)`, and is registered from `.genvm-tool.py` via
`ctx.collect_dir('tests/system/<name>', ...)` (discovery:
`genvm_tool/tests/stage/collection.py`). The project entry point overall is
`.genvm-tool.py` at the repo root (each executor line has its own).
