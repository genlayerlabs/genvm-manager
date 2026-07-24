# GenVM manager

Before doing build, test, submodule, release/versioning, or codegen work,
read the matching guide in `docs/contributing/howto/`:

- [genvm-tool.md](docs/contributing/howto/genvm-tool.md) — command overview, codegen data → generated files, plugin/test-definition layout
- [setup.md](docs/contributing/howto/setup.md) — first-time clone: submodule init, nix/direnv dev shell, third-party materialization
- [write-a-script.md](docs/contributing/howto/write-a-script.md) — conventions for new helper scripts: python + argparse, integrate into genvm-tool / support/ci

## Building
- [build.md](docs/contributing/howto/building/build.md) — debug build: configure + ninja, outputs, cargo quirks
- [docs.md](docs/contributing/howto/building/docs.md) — building the website, spec vs impl-spec, ADRs
- [runners.md](docs/contributing/howto/building/runners.md) — where runners come from (build vs download)

## Committing
- [git-third-party.md](docs/contributing/howto/committing/git-third-party.md) — how vendored trees (wasmtime, …) are tracked, edited, and committed as patches
- [runners.md](docs/contributing/howto/committing/runners.md) — runner dev-mode and hash hygiene before committing an executor line
- [submodules.md](docs/contributing/howto/committing/submodules.md) — manager + executor lines, gitlink bumps, pre-commit hook, push order

## Extending
- [add-host-function.md](docs/contributing/howto/extending/add-host-function.md) — new executor↔host protocol method
- [add-llm-provider.md](docs/contributing/howto/extending/add-llm-provider.md) — new LLM backend in the manager
- [add-wasi-function.md](docs/contributing/howto/extending/add-wasi-function.md) — new gl_call method or raw WASI function in an executor
- [modify-runner.md](docs/contributing/howto/extending/modify-runner.md) — runner dev-mode and hash refresh
- [modify-wasmtime.md](docs/contributing/howto/extending/modify-wasmtime.md) — patching vendored wasmtime, trap plumbing

## Releasing
- [release-build.md](docs/contributing/howto/releasing/release-build.md) — nix packages, platforms, release assets
- [versioning.md](docs/contributing/howto/releasing/versioning.md) — release trains, `.genvm-monorepo-root`, check-versions/branch-versions scripts

## Testing
- [README.md](docs/contributing/howto/testing/README.md) — `genvm-tool test` itself: filters, presets, continue files
- [fuzzing.md](docs/contributing/howto/testing/fuzzing.md) — AFL fuzz targets, host sysctl prep
- [integration.md](docs/contributing/howto/testing/integration.md) — running one executor line, `stable` (offline) subset, golden `.stdout`/`.hash` files, `--ignore-hash`, services
- [python.md](docs/contributing/howto/testing/python.md) — Python tests, direct pytest for genlayer-py-std
- [rust.md](docs/contributing/howto/testing/rust.md) — Rust tests, `#rust-test` shell, coverage
