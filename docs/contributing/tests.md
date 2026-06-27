# Running GenVM tests

GenVM uses the `genvm-tool test` runner (the language-agnostic ya-test-runner,
vendored into `genvm-tool`) for running all tests. The test runner automatically handles service dependencies (manager, modules, webdriver).

## Prerequisites

- Build the project first: `genvm-tool configure && ninja -C build`
- For `get_webpage` tests, a compatible webdriver is needed. Use the docker image: `./modules/webdriver/run-test-docker.sh`
- For `exec_prompt` tests, set the `OPENAIKEY` env variable to your OpenAI key

## Running Tests

### Using nix (recommended)
```bash
nix develop .#mock-tests --command genvm-tool test run
```

### Without nix

`genvm-tool test` needs `aiohttp` and `jsonnet` on the Python path. With those
installed, drive the in-tree wrapper directly:
```bash
# Run all tests
support/tools/genvm-tool/genvm-tool test run

# Run with tag filter
support/tools/genvm-tool/genvm-tool test --filter-tag 'stable' run

# Show available tests
support/tools/genvm-tool/genvm-tool test show test

# Show execution plan
support/tools/genvm-tool/genvm-tool test show plan
```

### Using Presets

Tag expression presets are available in `tests/presets/`:
```bash
# Run release tests (integration & stable)
genvm-tool test --filter-tag "$(cat tests/presets/release.txt)" run

# Run rust tests (rust | integration)
genvm-tool test --filter-tag "$(cat tests/presets/rust.txt)" run

# Run python tests
genvm-tool test --filter-tag "$(cat tests/presets/python.txt)" run
```

### Coverage Collection

To collect coverage for Rust tests:
```bash
nix develop .#rust-test --command genvm-tool test --filter-tag rust --coverage run
```

### Re-running Failed Tests

When tests fail, the runner automatically writes the failed test names to a continue file at `build/test-artifacts/continue/<timestamp>-<random>`. To re-run only the failed tests:

```bash
# Use the continue file path shown in the failure summary
genvm-tool test --filter-continue 20260123-143052-abc123 run

# Or use a full path
genvm-tool test --filter-continue build/test-artifacts/continue/20260123-143052-abc123 run
```

### Useful Options

- `--filter-name REGEX` - Filter tests by name regex
- `--filter-tag EXPR` - Filter tests by tags (e.g., `stable & !slow`)
- `--filter-continue FILE` - Re-run only tests from a continue file (from a previous failed run)
- `--fail-fast` - Stop on first failure
- `--coverage` - Enable coverage collection for Rust tests
- `--log-level {trace,debug,info,warning,error}` - Set logging level

## Test Categories

- **Integration tests** (`tests/cases/`): End-to-end tests using jsonnet configuration
- **Rust tests** (`executor/tests/`): Unit tests for the Rust executor
- **Python tests** (`runners/genlayer-py-std/test/`): Tests for the Python SDK

## Configuration

The test runner reads configuration from the monorepo marker `.genvm-monorepo-root`
in the project root:

```json
{
    "version": "v0.3",
    "active-versions": ["v0.3"],
    "artifacts_dir": "build/test-artifacts",
    "extra_python_paths": ["tests/plugins", "tests/runner"]
}
```

- `artifacts_dir` - Directory for test artifacts (logs, continue files). Defaults to `build/test-artifacts`.
- `extra_python_paths` - Paths prepended to `sys.path` so the suite can import its plugins.

The suite itself (collectors, plugins, CLI args) is the `tests` function exported
from `.genvm-tool.py` at the project root.
