#!/usr/bin/env bash

export PATH="$NIX:$PATH"

set -ex

echo "::group::genvm-tool configure"
genvm-tool configure
echo "::endgroup::"

echo "::group::ninja build (all/bin)"
ninja -v -C build all/bin
echo "::endgroup::"

echo "::group::post-install"
python3 ./build/out/bin/post-install.py \
    --error-on-missing-executor=false \
    --default-download=false
echo "::endgroup::"

echo "::group::rust tests"
nix develop '.?submodules=1#mock-tests' --command genvm-tool test run --ci --filter-tag "$(cat tests/presets/rust.txt)"
echo "::endgroup::"
