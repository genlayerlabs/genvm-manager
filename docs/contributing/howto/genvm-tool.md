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
| `docs` | render the `docs/contributing/` indexes, the source of the list in `CLAUDE.md` |
| `git` | `ls`, `list-repo`, `create-branches`, `check-for-push` across the manager and every executor submodule ([submodules.md](committing/submodules.md)) |

## Codegen

`ninja -C build codegen` regenerates from data JSON; never edit an output by
hand. Host protocol data is per line, public ABI data comes from the primary one

| Data | Generated |
|---|---|
| `executors/<line>.x/executor/codegen/data/host-fns.json` | that line's `executor/crates/common/src/host_fns.rs`, `tests/runner/origin/host_fns.py` |
| `executors/<primary>.x/executor/codegen/data/public-abi.json` | `tests/runner/origin/public_abi.py`, `docs/website/src/spec/appendix/constants.rst` |

## Suite Layout

The suite is `tests(ctx)` in the root `.genvm-tool.py`, which registers
collectors. Plugins in `tests/runner/genvm_tool_plugins/` are plain importable
modules, with no runner-specific registry. Per-suite definitions live next to
the tests in `tests/system/<name>/test.py`, pulled in with `ctx.collect_dir`.
Each executor line carries its own `.genvm-tool.py`
