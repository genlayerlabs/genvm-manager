#!/usr/bin/env bash

export PATH="$NIX:$PATH"

set -ex

support/tools/genvm-tool/genvm-tool configure

nix develop .#mock-tests --command genvm-tool test run --filter-tag "$(cat tests/presets/rust-fuzz.txt)"
