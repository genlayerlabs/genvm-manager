# Python Tests

```bash
genvm-tool test run --filter-tag python
```

Two suites: `genlayer-py-std` (in the primary executor line) and `support/ci`
(`unit_tests/`, the stdlib-only CI tools). The interpreter comes from a pinned
standalone flake, so pytest can be run directly, without the dev shell:

```bash
cd executors/v0.3.x/runners/genlayer-py-std
env_dir="$(nix build --no-link --print-out-paths path:../../support/nix/py-test)"
PYTHONPATH="$PWD/src:$PWD/src-emb" "$env_dir/bin/pytest" tests/
```

Coverage is enforced (`--cov-fail-under=75`), and the numpy-dependent tests need
that nix environment

For `support/ci` the same env works with no PYTHONPATH: `cd support/ci &&
"$env_dir/bin/pytest"` (its `conftest.py` puts the tools on the path)
