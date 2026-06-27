#!/usr/bin/env bash

export PATH="$NIX:$PATH"

set -ex

support/tools/genvm-tool/genvm-tool configure

ninja -v -C build all/bin

python3 ./build/out/bin/post-install.py \
    --error-on-missing-executor=false \
    --default-download=false

nix develop .#mock-tests --command genvm-tool test run --filter-tag "$(cat tests/presets/rust.txt)"
