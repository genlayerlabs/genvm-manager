# How-to guides

- [setup.md](setup.md) — first-time clone: submodule init, nix/direnv dev shell, third-party materialization.
- [genvm-tool.md](genvm-tool.md) — command overview, codegen data → generated files, plugin/test-definition layout.

## building/
- [build.md](building/build.md) — debug build: configure + ninja, outputs, cargo quirks.
- [runners.md](building/runners.md) — where runners come from (build vs download).
- [docs.md](building/docs.md) — building the website, spec vs impl-spec, ADRs.

## testing/
- [testing/README.md](testing/README.md) — `genvm-tool test` itself: filters, presets, continue files.
- [rust.md](testing/rust.md) — Rust tests, `#rust-test` shell, coverage.
- [python.md](testing/python.md) — Python tests, direct pytest for genlayer-py-std.
- [integration.md](testing/integration.md) — running one executor line, `stable` (offline) subset, golden `.stdout`/`.hash` files, `--ignore-hash`, services.
- [fuzzing.md](testing/fuzzing.md) — AFL fuzz targets, host sysctl prep.

## committing/
- [submodules.md](committing/submodules.md) — manager + executor lines, gitlink bumps, pre-commit hook, push order.
- [git-third-party.md](committing/git-third-party.md) — how vendored trees (wasmtime, …) are tracked, edited, and committed as patches.

## releasing/
- [versioning.md](releasing/versioning.md) — release trains, `.genvm-monorepo-root`, check-versions/branch-versions scripts.
- [release-build.md](releasing/release-build.md) — nix packages, platforms, release assets.

## extending/
- [add-llm-provider.md](extending/add-llm-provider.md) — new LLM backend in the manager.
- [add-wasi-function.md](extending/add-wasi-function.md) — new gl_call method or raw WASI function in an executor.
- [add-host-function.md](extending/add-host-function.md) — new executor↔host protocol method.
- [modify-runner.md](extending/modify-runner.md) — runner dev-mode and hash refresh.
- [modify-wasmtime.md](extending/modify-wasmtime.md) — patching vendored wasmtime, trap plumbing.
