# Modifying a Runner

Paths are relative to `executors/v0.3.x/`, the line that owns runner sources

1. Set `runners/support/versions/dev-mode.nix` to `true` and the runner's hash
	 in `runners/support/versions/current.nix` to `"test"`, then develop and test
	 Hashes move, so integration tests need `--ignore-hash`
	 ([integration.md](../testing/integration.md))
2. Set dev-mode back to `false`, and the changed runner's hash, plus its
	 dependents', to `null`
3. Stage the runner changes and run pre-commit; fix and restage them before
	 hash discovery so the hooks and Nix inspect the same source tree
4. Refresh the hashes with `runners/support/versions/hash-updater.py`
5. Stage `current.nix` and rebuild to confirm no mismatch remains

Step 4 in detail, and why a hash is never restored by hand:
[committing/runners.md](../committing/runners.md)

## Gating a Runner

A pin in `current.nix` may set 2 optional attributes, which end up in the
line's `data/all.json`:

1. `available-after = "2026-12-01T00:00:00Z";` — transactions timestamped
   earlier resolve the runner as malformed, the same as on an executor that does
   not ship it ([spec](../../../website/src/spec/02-execution-environment/04-runners.rst)).
   Pick a time by which every validator runs the new executor
2. `supersedes = "<gvm32 hash>";` — the hash of the same id this one replaces.
   The old hash must still be shipped, or evaluation fails

`hash-updater.py` rewrites only `hash`, so both survive a re-pin
