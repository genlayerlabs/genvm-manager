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
