---
name: test
description: Runs tests for the GenVM project. Use after making code changes to verify correctness.
---

# Running Tests

GenVM uses `genvm-tool test` for all tests. Before running tests, ensure the project is built (see `/build` skill).

> **Commands assume you're inside the project dev shell** — `nix develop
> '.?submodules=1#full'`, usually auto-loaded by direnv. `.#full` is a superset
> of the test shells, so these commands run bare inside it. (If a **Rust** test
> fails to load a shared lib, use the dedicated shell instead:
> `nix develop '.?submodules=1#rust-test' --command genvm-tool test run --filter-tag rust`.)
> `?submodules=1` is required on any flake ref — see `/submodules`.

## Quick Start

Run all tests:
```bash
genvm-tool test run
```

Run release tests (stable integration):
```bash
genvm-tool test run --filter-tag "$(cat tests/presets/release.txt)"
```

Run a specific test:
```bash
genvm-tool test run --filter-name 'test_name'
```

## genvm-tool test Commands

### Run Tests
```bash
genvm-tool test run [OPTIONS]
```

**Options:**
| Flag | Description |
|------|-------------|
| `--filter-name REGEX` | Filter tests by name regex |
| `--filter-tag EXPR` | Filter tests by tags (e.g., `stable & !slow`) |
| `--filter-continue FILE` | Re-run only tests from a continue file |
| `--fail-fast` | Stop execution after first failure |
| `--coverage` | Enable coverage collection for Rust tests |
| `--log-level LEVEL` | Set log level (trace/debug/info/warning/error) |
| `--ignore-hash` | Skip `.hash` (execution-hash) comparison entirely |

**`--ignore-hash`:** integration cases compare both the printed semantics
(`.N.stdout`) and a deterministic execution hash (`.N.hash`). When adding a new
case, you usually don't have a correct `.hash` yet — run with `--ignore-hash` so
only the `.stdout` semantics are checked, or set `stable_hash: false` on the
entry (then validators compare to the leader's hash instead of a committed file).

### Show Information
```bash
# Show available tests
genvm-tool test show test

# Show execution plan
genvm-tool test show plan

# Show available services
genvm-tool test show services

# Show available tags
genvm-tool test show tags
```

## Test Presets

Presets are tag expressions stored in `tests/presets/`:

| Preset | Expression | Use Case |
|--------|------------|----------|
| `release.txt` | `integration & stable` | CI release tests |
| `rust.txt` | `rust \| integration` | Rust development |
| `python.txt` | `python` | Python SDK tests |

Usage:
```bash
genvm-tool test --filter-tag "$(cat tests/presets/release.txt)" run
```

## Test Categories

### Integration Tests (`tests/cases/`)
End-to-end tests using jsonnet configuration. Services (manager, modules, webdriver) are started automatically.

```bash
genvm-tool test run --filter-tag integration
```

### Rust Tests
Cargo tests for Rust crates:
```bash
genvm-tool test run --filter-tag rust
```

With coverage:
```bash
genvm-tool test run --filter-tag rust --coverage
```

### Python Tests
Tests for the Python standard library (`genlayer-py-std`):
```bash
genvm-tool test run --filter-tag python
```

Or directly with pytest from the standalone py-test flake (no nix develop needed):
```bash
cd runners/genlayer-py-std
env_dir="$(nix build --no-link --print-out-paths path:../../support/nix/py-test)"
PYTHONPATH="$PWD/src:$PWD/src-emb" "$env_dir/bin/pytest" tests/
```

**Coverage:** pytest is configured with `--cov` and `--cov-fail-under=75`. Coverage scope includes `genlayer.types`, `genlayer.calldata`, `genlayer.storage`, `genlayer.evm`, `genlayer._internal`, and `genlayer_embeddings`. Must run inside nix develop for numpy-dependent tests and correct coverage resolution.

## Webdriver Setup

For web-related tests (semi-stable/unstable), webdriver is started automatically by genvm-tool test. To manually start it:

```bash
bash modules/webdriver/build-and-run.sh
```

## Precompile (Optional)

If WASM files or compilation changed, precompile to save test time:
```bash
./build/out/bin/genvm precompile
```

## Re-running Failed Tests

When tests fail, genvm-tool test writes failed test names to `build/test-artifacts/continue/<timestamp>-<random>`. Re-run only failed tests:

```bash
# Use filename shown in failure summary
genvm-tool test run --filter-continue 20260123-143052-abc123
```

## Quick Reference

| What to test | Command |
|--------------|---------|
| All tests | `genvm-tool test run` |
| Release tests | `genvm-tool test run --filter-tag "$(cat tests/presets/release.txt)"` |
| Rust tests | `genvm-tool test run --filter-tag rust` |
| Python (direct) | `cd runners/genlayer-py-std && PYTHONPATH="$PWD/src:$PWD/src-emb" "$(nix build --no-link --print-out-paths path:../../support/nix/py-test)/bin/pytest" tests/` |
| Re-run failed | `genvm-tool test run --filter-continue <file>` |
| With debug logs | `genvm-tool test run --log-level debug` |
| Show test list | `genvm-tool test show test` |

## See also

- `/build` — build before testing.
- `/rust-test-style` — conventions for writing Rust tests (`--filter-tag rust`).
- `/submodules` — the dev shell / `?submodules=1` and the multi-repo layout.
