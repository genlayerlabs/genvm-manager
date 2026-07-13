# Modifying a runner

Runner sources and version pins live per executor line; paths below are
relative to `executors/<line>.x/`.

1. Set `runners/support/versions/dev-mode.nix` to `true` and the runner's hash
   in `runners/support/versions/current.nix` to `"test"`; develop and test.
2. Set dev-mode back to `false`; set the changed runner's hash (and its
   dependents') to `null`.
3. Refresh hashes with `runners/support/versions/hash-updater.py` (run from
   anywhere inside the repo): it builds `#runners-all` with `--keep-going`,
   writes every reported `hash mismatch … got:` value back into `current.nix`,
   and repeats until fixed point. Hashes depend on dependencies' resolved uids,
   so they must be discovered in dependency order — the script handles that.

   Manual alternative: `ninja -C build all`, copy the `got:` hash from the
   error into `current.nix`, repeat per dependent runner.
4. Rebuild to verify no mismatches remain.
