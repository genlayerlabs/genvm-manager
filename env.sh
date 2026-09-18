#!/usr/bin/env bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# The tool comes from the pinned flake input, so outside the dev shell it has to
# be realized first. Already on PATH inside any dev shell
if ! command -v git-third-party > /dev/null
then
    git_third_party_out=$(nix build --no-link --print-out-paths "$SCRIPT_DIR?submodules=1#git-third-party")
    export PATH="$git_third_party_out/bin:$PATH"
    unset git_third_party_out
fi

if [ -f "$SCRIPT_DIR/.env" ]
then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi
