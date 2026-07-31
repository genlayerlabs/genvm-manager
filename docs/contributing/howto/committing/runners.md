# Committing Runner Changes

Runner sources and version pins live in the forward-rolling executor line,
`executors/v0.3.x/`; every path below is relative to it. Frozen v0.2.x has no
`runners/support/versions` tree — it ships a committed registry instead

During development `runners/support/versions/dev-mode.nix` may be `true` and
hashes in `runners/support/versions/current.nix` may be `"test"`. **Neither may
reach a commit**; a pre-commit guard rejects dev-mode

Before committing:

1. Set `dev-mode.nix` back to `false`
2. Set every `"test"` hash in `current.nix` to `null`
3. Run `runners/support/versions/hash-updater.py`, from anywhere inside the
   repo. It builds the umbrella's `#runners-all` with `--keep-going`, writes
   every reported `got:` value back into `current.nix`, and repeats until a
   fixed point. Hashes are content-addressed and depend on resolved dependency
   uids, so they must be discovered bottom-up, which the script handles. It
   needs the executor nested under the manager umbrella; a standalone checkout
   cannot build `runners-all`
4. Commit once every hash is a real `sha256-…` value

**Never restore an old hash by hand.** It becomes wrong the moment any source or
dependency changes, and only `hash-updater.py` produces a correct one

The full modification workflow:
[modify-runner.md](../extending/modify-runner.md)
