#!/usr/bin/env bash

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
source "$SCRIPT_DIR/_common.sh"

export ORIGINAL_PATH="$PATH"
export ORIGINAL_LD_LIBRARY_PATH="$LD_LIBRARY_PATH"

echo "::group::runner registry manifests"
mkdir -p build/out/executor/vTEST/data

# gvm32 (Crockford Base32) is the encoding the executor uses for runner paths;
# it is carried in `x.uid` as `id:gvm32hash`. Do NOT use `toHashFormat = "nix32"`
# here: Nix base32 has a different alphabet, so the registry names would never
# match the on-disk tars / GCS objects (e.g. nix32 `04l343…` vs gvm32 `5tnhg…`).
nix eval --verbose --impure --read-only --show-trace --json --expr \
    'let drv = import ./executors/v0.3.x/runners { host-system = builtins.currentSystem; } ; in builtins.listToAttrs (builtins.map (x: { name = x.id; value = builtins.head (builtins.match "[^:]+:(.*)" x.uid); }) drv)' \
    > build/out/executor/vTEST/data/latest.json

nix eval --verbose --impure --read-only --show-trace --json --expr \
    'let drv = import ./executors/v0.3.x/runners { host-system = builtins.currentSystem; } ; in builtins.listToAttrs (builtins.map (x: { name = x.id; value = [ (builtins.head (builtins.match "[^:]+:(.*)" x.uid)) ]; }) drv)' \
    > build/out/executor/vTEST/data/all.json
echo "::endgroup::"

echo "::group::download runners"
# we can't run it within nix because it uses `nix add` which sigsegvs
python3 ./support/runner-script.py \
    download \
    --nix-preload --allow-partial --dest build/out/runners --registry build/out/executor/vTEST/data/all.json
echo "::endgroup::"

echo "::group::build runners-all"
nix build -v -L -o build/out-runners '.?submodules=1#runners-all'
mkdir -p ./build/out/runners
# Symlink the nix-store runners into build/out/runners (real dirs, symlinked
# files) rather than copying; -f overlays them onto any files fetched by the
# download step above. Absolute source (readlink -f) so the links point straight
# into the store.
cp -rsf "$(readlink -f build/out-runners)"/. ./build/out/runners/.
echo "::endgroup::"

echo "::group::upload runners"
nix develop '.?submodules=1#rust-test' --command python3 \
    ./support/runner-script.py \
    upload \
    --root build/out/runners --registry build/out/executor/vTEST/data/all.json || true
echo "::endgroup::"

# src/test-rust.sh emits its own log groups; do not wrap it in one (they cannot nest).
nix develop '.?submodules=1#rust-test' --command bash ./support/ci/pipelines/src/test-rust.sh
