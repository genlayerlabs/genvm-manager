# Modifying a Runner

Paths are relative to the target line, `executors/v0.<N>.x/`; each forward-rolling
line owns its runner sources and pins

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

1. `fork = "genvm/executor/v0.<N>/runners-<batch>";` — the
   [fork](../../../website/src/spec/01-core-architecture/03-versioning.rst) that
   makes the runner visible. Without it the pin gets the line's own fork, from
   `manifest.json`. A runner new to a released line must name a fork of its own
   ([spec](../../../website/src/spec/02-execution-environment/04-runners.rst)),
   or it activates for every network the moment validators upgrade
2. `supersedes = "<gvm32 hash>";` — the hash of the same id this one replaces.
   The old hash must still be shipped, or evaluation fails

`hash-updater.py` rewrites only `hash`, so both survive a re-pin

### Adding a Fork

The schedule is `install/data/genvm-chainforks.json` in the manager repo; a fork
named by a pin but missing there fails the build. It holds only `dev`: real
networks live in each host's own schedule (the node's, Studio's), which the host
passes as `genvm-manager --chainforks-path <file> --default-network <network>`,
both for serving and for `check-install`. A flag left out falls back to the
config's `chainforks_path` or `default_network`. The shipped `default_network`
is null, so a request naming no network is rejected unless the host passes one

1. Add the fork under `forks`, with `"requires": ["genvm/executor/<line>"]`: a
   name extending another fork's name must require it
2. Schedule it `always` on `dev`, so the test suite sees it. A network missing
   the fork fails the build and the manager startup
3. Add the same fork to each host's schedule, scheduled on every network there
   (`never` or the agreed RFC 3339 date on the real ones),
   before the host moves to this release: the manager and `check-install`
   reject a schedule missing a fork the manifest or a runner names. A host may
   define forks of its own, such as `node/*`, that no runner names; the manager
   accepts them and passes them on in `--forks`, where the executor ignores them
4. Integration tests run on `dev`, where every fork is active; test the closed
   gate in the executor, by leaving the fork out of `genvm run --forks`
