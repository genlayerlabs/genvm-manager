# GenVM manager

Before doing build, test, submodule, release/versioning, or codegen work,
read the matching guide in `docs/contributing/howto/`:

- [setup.md](docs/contributing/howto/setup.md) — first-time clone, nix/direnv dev shell
- [genvm-tool.md](docs/contributing/howto/genvm-tool.md) — genvm-tool commands, codegen, plugins
- [building/build.md](docs/contributing/howto/building/build.md) — debug build: configure + ninja
- [building/runners.md](docs/contributing/howto/building/runners.md) — getting runners
- [building/docs.md](docs/contributing/howto/building/docs.md) — website/spec docs, ADRs
- [testing/README.md](docs/contributing/howto/testing/README.md) — `genvm-tool test`, continue files (+ [rust.md](docs/contributing/howto/testing/rust.md), [python.md](docs/contributing/howto/testing/python.md), [integration.md](docs/contributing/howto/testing/integration.md), [fuzzing.md](docs/contributing/howto/testing/fuzzing.md))
- [committing/submodules.md](docs/contributing/howto/committing/submodules.md) — multi-repo commits, gitlinks, hooks, push order
- [committing/runners.md](docs/contributing/howto/committing/runners.md) — runner hash hygiene before committing (dev-mode, hash-updater.py)
- [committing/git-third-party.md](docs/contributing/howto/committing/git-third-party.md) — vendored wasmtime/wasm-tools patch workflow
- [releasing/versioning.md](docs/contributing/howto/releasing/versioning.md) — release trains, `.genvm-monorepo-root`, version scripts
- [releasing/release-build.md](docs/contributing/howto/releasing/release-build.md) — nix packages, release assets
- [extending/](docs/contributing/howto/README.md#extending) — add LLM provider / WASI function / host function, modify a runner or wasmtime
