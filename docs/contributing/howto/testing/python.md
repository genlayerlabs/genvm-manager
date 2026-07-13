# Python tests

```bash
genvm-tool test run --filter-tag python
```

## Direct pytest (genlayer-py-std, no dev shell)

```bash
cd runners/genlayer-py-std
env_dir="$(nix build --no-link --print-out-paths path:../../support/nix/py-test)"
PYTHONPATH="$PWD/src:$PWD/src-emb" "$env_dir/bin/pytest" tests/
```

Coverage is enforced (`--cov-fail-under=75`); numpy-dependent tests need the
nix env.
