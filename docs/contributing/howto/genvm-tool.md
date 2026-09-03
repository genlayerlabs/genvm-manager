# genvm-tool

The umbrella tool, at `support/tools/genvm-tool`, on `PATH` in the dev shell.
Full reference: `genvm-tool --print-manpage | man -l -`. Global options go
before the subcommand: `genvm-tool --log-level debug test run …`. Outside the
dev shell run `build/genvm_tool.sh`, which `configure` writes — the source tree
has no wrapper

| Command | Effect |
|---|---|
| `configure` | write `build/build.ninja` and `build/info.json` |
| `test` | run the suite ([testing/README.md](testing/README.md)) |
| `codegen` | render a data JSON into rust/python/rst/go |
| `build-manifest` | write the manager's `data/manifest.yaml` |
| `docs` | render the `docs/contributing/` indexes; `--write` splices them into `AGENTS.md` (`CLAUDE.md` is a symlink to it), `--check` fails when that copy is stale |
| `fuzz-corpus` | seed the fuzz corpora from a finished run ([testing/fuzzing.md](testing/fuzzing.md)) |
| `git` | `ls`, `list-repo`, `create-branches`, `check-for-push` across the manager and every executor submodule ([submodules.md](committing/submodules.md)) |

## Codegen

`ninja -C build codegen` regenerates from data JSON; never edit an output by
hand. Host protocol data is shared, per-line data lives in
`executors/<line>.x/executor/codegen/data/`

| Data | Generated |
|---|---|
| `crates/modules-interfaces/codegen/data/host-fns.json` | `…/src/host_fns.rs`, `tests/runner/origin/host_fns.py` |
| `crates/modules-interfaces/codegen/data/manager-api.json` | `…/src/manager_api.rs`, `tests/runner/origin/manager_api.py`, `docs/website/src/impl-spec/appendix/manager-socket-consts.rst` |

Per line, with the manager-global outputs taken from the primary line only:

| Data | Per-line output | From the primary line only |
|---|---|---|
| `public-abi.json` | `executor/crates/sdk-rs/src/abi/consts.rs`, `runners/genlayer-py-std/src/genlayer/vm/public_abi.py` | `tests/runner/origin/public_abi.py`, `docs/website/src/spec/appendix/constants.rst` |
| `public-abi-pending.json` | `executor/crates/common/src/public_abi_pending.rs` | `docs/website/src/spec/appendix/constants-pending.rst` |
| `internal-constants.json` | `executor/crates/common/src/internal_constants.rs` | `docs/website/src/spec/appendix/internal-constants.rst` |

## Suite Layout

The suite is `tests(ctx)` in the root `.genvm-tool.py`, which registers
collectors. Plugins in `tests/runner/genvm_tool_plugins/` are plain importable
modules, with no runner-specific registry. Per-suite definitions live next to
the tests in `tests/system/<name>/test.py`, pulled in with `ctx.collect_dir`, or
reuse `integration_test_directory` for contract-execution Jsonnet. Each
executor line carries its own `.genvm-tool.py`

Services that bind the same listener declare the same `ctx.new_semaphore`
value. The scheduler finishes a service and its dependent cases before starting
the next service holding that semaphore; `genvm-tool test show plan` displays
the resulting lifetime boundaries
