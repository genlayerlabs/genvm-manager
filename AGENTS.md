# GenVM manager

Before changing this repository, read the matching page in `docs/contributing/`:
a task has a how-to, a design question has an explanation

## Review-Ready

The bar every change must clear before you hand it back. It is called
**Review-Ready, not Done** — "done" runs on through Darien's review, CI e2e, merge
and release. Report **Review-Ready**, never "done"; it is binary, no partial
credit. If any item fails it is not Review-Ready — say so plainly and name the
blocker.

### Intent

1. **It does what was asked.** Restate the requirement in the PR and trace each
   part to the evidence showing it met. (Guards against a perfectly-tested
   implementation of the wrong thing.)
2. **Prefer one thing**, small enough to review in one sitting — a preference,
   NOT a gate; not always possible or convenient. What IS firm: a defect you find
   in your OWN work you fix immediately, without asking — and if you have a
   concern, ASK. A defect outside the diff gets recorded, not silently folded in
   and not dropped.

### Proof

3. **Nothing goes to a PR untested.**
4. **Everything testable locally is tested locally, before pushing** — lint, unit,
   integration, conformance, compile. In this repo that is the pre-commit hooks
   (`./support/ci/run.sh pipeline commit-hooks`), the Rust and Python suites plus
   the integration/system cases via `genvm-tool test run`, and a debug build
   (`genvm-tool configure && ninja -C build`) when code changed — see
   [testing](docs/contributing/howto/testing/README.md) and
   [building](docs/contributing/howto/building/build.md) (runner artifacts build on
   Linux, not native macOS). CI is not our test runner: it costs real money, so
   anything reproducible locally is caught locally; CI is for what genuinely cannot
   be.
5. **Targeted feature coverage.** Map the change to the affected tests and run them
   locally before pushing — for VM, executor or protocol changes that is the
   relevant integration/system cases (`genvm-tool test run --filter-tag ...`) and,
   for behaviour that can drift nondeterministically, an agentic-fuzzing pass.
   Never fewer than the tests covering the changed surface — the risk is always
   underestimating.

### The artifact

6. **Read the entire diff yourself** — every line, including generated and vendored
   changes you caused.
7. **No known defects left in the diff** — including comments, help text and docs
   that assert something false. A WRONG COMMENT IS A DEFECT.
8. **Docs, the `genvm-tool` man page and `--help` match actual behaviour, same
   PR** — including the spec or impl-spec page under `docs/website/src` when the
   change touches documented VM or protocol behaviour, and the matching
   `docs/contributing/` how-to or explanation page for a workflow change.
9. **Operationally ready.** Security surface considered (new external surface,
   secrets/keys, dependency and vendor-hash changes); new behaviour diagnosable from
   metrics/logs without reading source; config-schema compatibility and a way to
   back it out. State explicitly when a dimension does not apply.

### The boundary

10. **Review-Ready = the PR is open and its checks are green — without CI e2e**,
    unless Darien requests it. He reviews and triggers CI e2e himself.
11. **This list is inspected and adapted.** Every escaped defect asks: which line
    would have caught it, and why did it not?

**Approval — needs Darien's go:** merges, `--admin`, CI e2e runs, and
external/irreversible actions outside the PR (Slack, releases, deploys).
**Forbidden, never even ask:** force-push to a shared branch (`vX.Y`, `vX.Y-dev`) —
branch protection blocks it too.
**Not gated:** force-pushing your own PR branch (rebasing is routine); fixing your
own defects.

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
- [fuzz.md](docs/contributing/explanation/fuzz.md) — why fuzz targets get fake entropy and no CmpLog
- [merge-model.md](docs/contributing/explanation/merge-model.md) — why merging is a maintainer panel rather than a merge queue
- [shared-submodule-cache.md](docs/contributing/explanation/shared-submodule-cache.md) — why submodules are worktrees of one cache repo, not clones
- [vendored-trees.md](docs/contributing/explanation/vendored-trees.md) — why third-party sources are patch series, not forks
