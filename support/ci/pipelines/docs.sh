#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source "$SCRIPT_DIR/_common.sh"

echo "::group::generate runner-versions"
COPYRIGHT_YEAR="$(git log -1 --date=format:%Y --format=%ad)"
export COPYRIGHT_YEAR

python3 ./docs/website/generate.py docs/website/src/impl-spec/appendix/runners-versions.json
echo "::endgroup::"

# `?submodules=1`: the manager flake imports the executor lines, so without it
# the executors/<line>.x paths are not in the flake source tree and evaluation
# fails ("not tracked by Git").
# src/docs.sh emits its own log groups; do not wrap it in one (they cannot nest).
nix develop -i '.?submodules=1#gen-docs' --command bash ./support/ci/pipelines/src/docs.sh
