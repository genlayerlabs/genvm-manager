#!/usr/bin/env bash

export PATH="$NIX:$PATH"

set -ex

genvm-tool configure

ninja -v -C build all/bin

python3 ./build/out/bin/post-install.py \
    --error-on-missing-executor=false \
    --default-download=false

nix develop '.?submodules=1#mock-tests' --command genvm-tool test run --ci --filter-tag "$(cat tests/presets/rust.txt)"
