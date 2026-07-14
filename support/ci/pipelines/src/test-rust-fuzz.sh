#!/usr/bin/env bash

export PATH="$NIX:$PATH"

set -ex

echo "::group::genvm-tool configure"
genvm-tool configure
echo "::endgroup::"

echo "::group::rust fuzz tests"
nix develop '.?submodules=1#mock-tests' --command genvm-tool test run --ci --filter-tag "$(cat tests/presets/rust-fuzz.txt)"
echo "::endgroup::"
