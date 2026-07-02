# Committing runner changes

Runner sources and version pins live inside the executor submodule
(`executors/<line>.x/`). All paths below are relative to that submodule root.

## Dev mode

During development `runners/support/versions/dev-mode.nix` may be `true` and
runner hashes in `runners/support/versions/current.nix` may be `"test"`.
**Neither must reach a commit.**

Before committing:

1. Set `dev-mode.nix` back to `false`.
2. Set "test" hash to `null` in `current.nix`.
3. Run `runners/support/versions/hash-updater.py` (from anywhere inside the
   repo) to recompute real hashes. It builds `#runners-all` with
   `--keep-going`, writes every reported `got:` value back into `current.nix`,
   and repeats until fixed point. Hashes are content-addressed and depend on
   resolved dependency uids, so they must be discovered bottom-up — the script
   handles that automatically.
4. Commit once all hashes are real `sha256-…` values.

**Never restore an old hash value manually.** It becomes wrong the moment any
source or dependency changes; only `hash-updater.py` produces a correct value.

See [modify-runner.md](../extending/modify-runner.md) for the full
runner-modification workflow.
