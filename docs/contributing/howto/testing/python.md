# Python Tests

```bash
genvm-tool test run --filter-tag python
```

The suite is `genlayer-py-std`, which lives in the primary executor line. The
interpreter comes from a pinned standalone flake, so pytest can be run directly,
without the dev shell:

```bash
cd executors/v0.3.x/runners/genlayer-py-std
env_dir="$(nix build --no-link --print-out-paths path:../../support/nix/py-test)"
PYTHONPATH="$PWD/src:$PWD/src-emb" "$env_dir/bin/pytest" tests/
```

Coverage is enforced (`--cov-fail-under=75`), and the numpy-dependent tests need
that nix environment
