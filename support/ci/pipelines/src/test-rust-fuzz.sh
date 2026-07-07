#!/usr/bin/env bash

export PATH="$NIX:$PATH"

set -ex

genvm-tool configure

nix develop '.?submodules=1#mock-tests' --command genvm-tool test run --ci --filter-tag "$(cat tests/presets/rust-fuzz.txt)"
