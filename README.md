# GenVM

[![standard-readme compliant](https://img.shields.io/badge/readme%20style-standard-brightgreen.svg)](https://github.com/RichardLitt/standard-readme) [![License: BSL 1.1](https://img.shields.io/badge/License-BSL%201.1-blue.svg)](./LICENSE) [![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white&style=flat)](https://discord.com/invite/qjCU4AWnKE) [![Telegram](https://img.shields.io/badge/Telegram-2CA5E0?style=flat&logo=telegram&logoColor=white)](https://t.me/genlayer) [![Twitter](https://img.shields.io/twitter/url/https/twitter.com/yeagerai.svg?style=social&label=Follow%20%40GenLayer)](https://x.com/GenLayer) [![GitHub star chart](https://img.shields.io/github/stars/genlayerlabs/genvm-manager?style=social)](https://star-history.com/#genlayerlabs/genvm-manager)

> The execution environment for Intelligent Contracts in the GenLayer protocol.

GenVM executes Intelligent Contracts — which can contain non-deterministic code —
while preserving blockchain security and consistency. This repository is the
umbrella (manager) that builds GenVM from its parts: the executor, the runners,
the modules, and the install/manifest pipeline.

## Table of Contents

- [Background](#background)
- [Install](#install)
- [Usage](#usage)
- [Maintainers](#maintainers)
- [Contributing](#contributing)
- [License](#license)

## Background

This is a monorepo for GenVM. It is composed of the following sub-projects:

- [`executors/`](./executors/) — the core GenVM executor (a [genvm-executor]
  submodule, pinned per version train) plus the [`runners/`](./executors/v0.3.x/runners/)
  available to contracts (software floating point, the Python interpreter with
  built-in bindings to the GenVM WASM module, the GenLayer Python standard
  library, ...). The executor is a modified [`wasmtime`](https://wasmtime.dev)
  that exposes the `genvm-sdk-wasi` implementation and performs all sandboxing.
- [`implementation/`](./implementation/) — the manager and modules (LLM, web).
- [`libs/`](./libs/), [`support/`](./support/) — shared libraries and build/CI tooling.

## Install

Required tools:

- git
- ruby (3.\*)
- ninja
- rustup (cargo + rustc)
- (for runners) nix and an x86_64 system

All of them (except git, for obvious reasons) are provided by the default shell in
`build-scripts/devenv/flake.nix` (for direnv add `use flake ./build-scripts/devenv`).

### Debug build

1. `cd $PROJECT_DIR`
2. `git submodule update --init --recursive --depth 1`
3. `source env.sh` (not needed if you used the flake)
4. `git third-party update --all`
5. `genvm-tool configure` — scrapes and configures all targets (similar to CMake).
   Outside the dev shell use `support/tools/genvm-tool/genvm-tool configure`.
6. `ninja -C build` (or `ninja -C build all/bin`) — output is at `build/out` as a
   root (`bin`, `share`)
7. Get `genvm-runners.zip` from [GitHub][genvm-manager]
8. Merge `build/out` and `genvm-runners.zip`

### Production build

> WARNING: currently supported only on x86_64 Linux hosts.

1. `cd $PROJECT_DIR`
2. `nix build -o build/out-universal -v -L .#all-for-platform.universal`
3. `nix build -o build/out-amd64-linux -v -L .#all-for-platform.amd64-linux`
4. Merge outputs

## Usage

GenVM's only purpose is to execute Intelligent Contracts. For getting-started
documentation, see the [GenLayer documentation](https://docs.genlayer.com/build-with-genlayer/intelligent-contracts).
For more complex examples, look into the [test suite](./executors/v0.3.x/tests/integration/).

## Maintainers

[GenLayer Labs](https://github.com/genlayerlabs).

## Contributing

PRs accepted. See the [contributing guide](./docs/contributing/README.md).

## License

[Business Source License 1.1](./LICENSE) © GenLayer Labs Corp.

[genvm-manager]: https://github.com/genlayerlabs/genvm-manager
[genvm-executor]: https://github.com/genlayerlabs/genvm-executor
