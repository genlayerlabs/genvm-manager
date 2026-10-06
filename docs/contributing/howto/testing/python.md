# Python Tests

```bash
genvm-tool test run --filter-tag python
```

Three suites: `genlayer-py-std` (in the primary executor line),
`support/tools/genvm-tool` and `support/ci` (each under `unit_tests/`; the CI
tools are stdlib-only). The interpreter comes from a pinned standalone flake, so
pytest can be run directly, without the dev shell:

```bash
cd executors/v0.3.x/runners/genlayer-py-std
env_dir="$(nix build --no-link --print-out-paths path:../../support/nix/py-test)"
PYTHONPATH="$PWD/src:$PWD/src-emb" "$env_dir/bin/pytest" tests/
```

Coverage is enforced (`--cov-fail-under=75`), and the numpy-dependent tests need
that nix environment

For `support/ci` the same env works with no PYTHONPATH: `cd support/ci &&
"$env_dir/bin/pytest"` (its `conftest.py` puts the tools on the path)

`support/tools/genvm-tool` has its own flake, and the package is imported from
the working tree:

```bash
cd support/tools/genvm-tool
env_dir="$(nix build --no-link --print-out-paths path:.)"
PYTHONPATH="$PWD" "$env_dir/bin/pytest" unit_tests/
```
