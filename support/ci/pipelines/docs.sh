#!/usr/bin/env bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source "$SCRIPT_DIR/_common.sh"

echo "::group::generate runner-versions"
COPYRIGHT_YEAR="$(git log -1 --date=format:%Y --format=%ad)"
export COPYRIGHT_YEAR

python3 ./docs/website/generate.py docs/website/src/impl-spec/appendix/runners-versions.json
echo "::endgroup::"

# src/docs.sh emits its own log groups; do not wrap it in one (they cannot nest).
nix develop -i .#gen-docs --command bash ./support/ci/pipelines/src/docs.sh
