#!/usr/bin/env bash

set -ex

cd ./executors/v0.3.x/runners/genlayer-py-std

echo "::group::build python test env"
# Dependency env comes from the standalone nix flake (was `poetry install`); the
# packages under test are imported from src/ + src-emb/ via PYTHONPATH (was the
# poetry editable install). Mirrors tests/runner/genvm_tool_plugins/pytest.py.
env_dir="$(nix build --no-link --print-out-paths path:../../support/nix/py-test)"
echo "::endgroup::"

echo "::group::pytest (genlayer-py-std)"
PYTHONPATH="$PWD/src:$PWD/src-emb${PYTHONPATH:+:$PYTHONPATH}" \
	"$env_dir/bin/pytest"
echo "::endgroup::"
