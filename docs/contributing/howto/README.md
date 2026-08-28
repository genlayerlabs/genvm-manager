# How-To Guides

- [setup.md](setup.md) — first-time clone: submodules, vendored trees, nix/direnv dev shell
- [genvm-tool.md](genvm-tool.md) — the umbrella tool: man page, test and git subcommands, codegen
- [pr.md](pr.md) — branch model, PR action panel, App-owned landing and executor projection
- [review-ready.md](review-ready.md) — the checks a change must pass before it is put up for review

## building/
- [build.md](building/build.md) — debug build: configure + ninja, targets, outputs, cargo quirks
- [runners.md](building/runners.md) — where runners come from: build on Linux, download elsewhere
- [docs.md](building/docs.md) — building and publishing the website, spec vs impl-spec, ADRs

## docs/
- [style.md](docs/style.md) — prose conventions for guides, specs, ADRs and commit bodies

## testing/
- [testing/README.md](testing/README.md) — `genvm-tool test`: filters, presets, continue files
- [rust.md](testing/rust.md) — Rust tests: where they go, how to run them, coverage
- [python.md](testing/python.md) — Python tests, direct pytest for genlayer-py-std
- [integration.md](testing/integration.md) — jsonnet cases, tags, golden `.stdout`/`.hash` files, services
- [fuzzing.md](testing/fuzzing.md) — AFL fuzz targets, seeding a corpus from a run, host sysctl prep

## committing/
- [submodules.md](committing/submodules.md) — repo topology, gitlink bumps, pre-commit hooks, push order
- [runners.md](committing/runners.md) — clearing runner dev-mode and refreshing hashes before a commit
- [git-third-party.md](committing/git-third-party.md) — how vendored trees (wasmtime, …) are pinned and patched

## releasing/
- [versioning.md](releasing/versioning.md) — release trains, `.genvm-monorepo-root`, version tools
- [release-build.md](releasing/release-build.md) — nix packages, platforms, release assets

## extending/
- [add-llm-provider.md](extending/add-llm-provider.md) — new LLM backend in the manager
- [add-wasi-function.md](extending/add-wasi-function.md) — new gl_call method or raw WASI function
- [add-host-function.md](extending/add-host-function.md) — new executor↔host protocol method
- [modify-runner.md](extending/modify-runner.md) — runner dev-mode and hash refresh
- [modify-wasmtime.md](extending/modify-wasmtime.md) — patching vendored wasmtime, trap plumbing
- [write-a-script.md](extending/write-a-script.md) — conventions for helper scripts and pre-commit hooks
- [add-a-skill.md](extending/add-a-skill.md) — agent skills in `.agents/`, the frontmatter trigger, the symlink, skill vs how-to
