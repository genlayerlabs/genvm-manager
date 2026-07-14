#!/usr/bin/env bash
# Run the git-hooks (pre-commit) checks across the manager and every executor
# submodule, in check mode, for CI.
#
# Why in-tree (`pre-commit run --all-files`) and not the sandboxed
# `nix build .#checks…pre-commit`: the executor crates reach the manager's
# shared `crates/` through `../../../../../` relative paths. Those resolve only
# in the real nested checkout (executor submodule under the manager), not in an
# isolated flake-source sandbox — so `cargo-fmt` (and anything else crossing the
# repo boundary) must run against the working tree. Each repo still supplies its
# own `pre-commit` binary + generated config from its own flake, so every repo
# is checked with its own pinned toolchain.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
cd "$SCRIPT_DIR/../../.."

SYSTEM=$(nix eval --impure --raw --expr 'builtins.currentSystem')

# Run one repo's hooks from inside its working tree.
#   $1 dir       — repo dir relative to the manager root
#   $2 label     — human label for the log group
#   $3 flakeref  — flake ref to read the pre-commit config/package from
run_repo() {
	local dir="$1" label="$2" flakeref="$3"
	echo "::group::pre-commit: $label"
	(
		cd "$dir"
		local cfg pc home
		cfg=$(nix build --no-link --print-out-paths "$flakeref#checks.$SYSTEM.pre-commit-check.config.configFile")
		pc=$(nix build --no-link --print-out-paths "$flakeref#checks.$SYSTEM.pre-commit-check.config.package")/bin/pre-commit
		# Isolated cache so CI never shares/pollutes a developer cache.
		home=$(mktemp -d)
		PRE_COMMIT_HOME="$home" "$pc" run \
			--all-files \
			--config "$cfg" \
			--hook-stage pre-commit \
			--show-diff-on-failure \
			--color always
	)
	echo "::endgroup::"
}

# The manager flake only evaluates with submodules present (it imports the
# executor lines); the executors are self-contained.
run_repo . manager ".?submodules=1"
run_repo executors/v0.3.x "executor v0.3.x" "."
run_repo executors/v0.2.x "executor v0.2.x" "."
