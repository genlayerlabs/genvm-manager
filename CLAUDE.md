# GenVM manager

Before changing this repository, read the matching page in `docs/contributing/`:
a task has a how-to, a design question has an explanation

<!-- below is generated with `genvm-tool docs` -->

## Tutorial
- [first-contribution.md](docs/contributing/tutorial/first-contribution.md) — patch an executor, branch, commit, push, open the PR

## Howto
- [genvm-tool.md](docs/contributing/howto/genvm-tool.md) — the umbrella tool: man page, test and git subcommands, codegen
- [pr.md](docs/contributing/howto/pr.md) — branch model, PR action panel, `ci-safe` / `run-full-tests`, merge gates
- [setup.md](docs/contributing/howto/setup.md) — first-time clone: submodules, vendored trees, nix/direnv dev shell

## Howto/Building
- [build.md](docs/contributing/howto/building/build.md) — debug build: configure + ninja, targets, outputs, cargo quirks
- [docs.md](docs/contributing/howto/building/docs.md) — building and publishing the website, spec vs impl-spec, ADRs
- [runners.md](docs/contributing/howto/building/runners.md) — where runners come from: build on Linux, download elsewhere

## Howto/Committing
- [git-third-party.md](docs/contributing/howto/committing/git-third-party.md) — how vendored trees (wasmtime, …) are pinned and patched
- [runners.md](docs/contributing/howto/committing/runners.md) — clearing runner dev-mode and refreshing hashes before a commit
- [submodules.md](docs/contributing/howto/committing/submodules.md) — repo topology, gitlink bumps, pre-commit hooks, push order

## Howto/Docs
- [style.md](docs/contributing/howto/docs/style.md) — prose conventions for guides, specs, ADRs and commit bodies

## Howto/Extending
- [add-host-function.md](docs/contributing/howto/extending/add-host-function.md) — new executor↔host protocol method
- [add-llm-provider.md](docs/contributing/howto/extending/add-llm-provider.md) — new LLM backend in the manager
- [add-wasi-function.md](docs/contributing/howto/extending/add-wasi-function.md) — new gl_call method or raw WASI function
- [modify-runner.md](docs/contributing/howto/extending/modify-runner.md) — runner dev-mode and hash refresh
- [modify-wasmtime.md](docs/contributing/howto/extending/modify-wasmtime.md) — patching vendored wasmtime, trap plumbing
- [write-a-script.md](docs/contributing/howto/extending/write-a-script.md) — conventions for helper scripts and pre-commit hooks

## Howto/Releasing
- [release-build.md](docs/contributing/howto/releasing/release-build.md) — nix packages, platforms, release assets
- [versioning.md](docs/contributing/howto/releasing/versioning.md) — release trains, `.genvm-monorepo-root`, version tools

## Howto/Testing
- [README.md](docs/contributing/howto/testing/README.md) — `genvm-tool test`: filters, presets, continue files
- [fuzzing.md](docs/contributing/howto/testing/fuzzing.md) — AFL fuzz targets, host sysctl prep
- [integration.md](docs/contributing/howto/testing/integration.md) — jsonnet cases, tags, golden `.stdout`/`.hash` files, services
- [python.md](docs/contributing/howto/testing/python.md) — Python tests, direct pytest for genlayer-py-std
- [rust.md](docs/contributing/howto/testing/rust.md) — Rust tests: where they go, how to run them, coverage

## Explanation
- [docs-layout.md](docs/contributing/explanation/docs-layout.md) — the 4 kinds of page and where each belongs
- [executor-lines.md](docs/contributing/explanation/executor-lines.md) — why several executor lines ship side by side, and what it costs
- [merge-model.md](docs/contributing/explanation/merge-model.md) — why merging is a maintainer panel rather than a merge queue
- [shared-submodule-cache.md](docs/contributing/explanation/shared-submodule-cache.md) — why submodules are worktrees of one cache repo, not clones
- [vendored-trees.md](docs/contributing/explanation/vendored-trees.md) — why third-party sources are patch series, not forks
