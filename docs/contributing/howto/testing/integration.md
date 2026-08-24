# Integration Tests

```bash
genvm-tool test run --filter-tag integration
```

Cases are `.jsonnet` files in `executors/<line>.x/tests/integration/`; each
yields a `<name>/prepare` case plus one per step (`/0l`, `/0v`, `/0s`, …), and
a sibling `.skip` file marks a case skipped. Every line in `active-versions`
runs its own cases against its own built executor, via `build/info.json`

A step may declare `executor_routes: {'<address>': <route>}` to send a
`CallContract` on that address to another executor line instead of running it
in-process — the mock host answers `resolve_call_contract_executor` with that
route, and the manager spawns the nested run. A route is a major (an integer,
resolved by the manifest's rules) or a version string naming the line outright,
`re:`-prefixed to match manifest keys rather than name a directory. Prefer a
version: every released line is semver major `0`, so a major cannot pick
between them and resolves to the newest one. Omit the whole key and every call
stays in-process, which is what all other cases do. A routed callee must pin
its runner by hash: the nested executor runs with debug mode disabled, where
`:test` does not resolve. See `misc/routed_call` on v0.3.x

A multi-line system case may also set `reroute_to` on an individual step, for
example while deploying a fixture with the executor line that owns its public
ABI. Use `${executorV02}` or `${executorV03}` rather than pinning a release
version; the collector unfolds these from `build/info.json`

Set `expected_executor_route_requests` to assert the exact hook calls. Each
entry has `contract_address`, numeric `state_mode`, and `advisory_major`

A step's declared public-ABI `major` is read from the contract's root slot, so
the manager routes it the way a real host would. A step may set `major`
explicitly when its subject is what the executor does with a major the manager
would otherwise refuse to route — see `runner/major_mismatch`

## Tags

| Tag | Meaning |
|---|---|
| `stable` | no LLM keys and no webdriver, runs offline; the executor is told `no_modules` |
| `semi-stable`, `unstable` | need modules and webdriver; `unstable` is retried up to 3 times |
| `bench` | benchmarks, excluded from the presets |
| `feature-*` | generated from the case's feature paths |

Stability is the second argument of `util.features(paths, stability)` in the
case's jsonnet `tags`; legacy v0.2.x uses a `stable/` top-level directory
instead. `--filter-tag` accepts alphanumeric tag names containing `-` and `_`;
matching remains exact

## Running a Subset

1. One line: `--filter-name 'executors/v0.3.x/'`, an unanchored regex over the
   repo-relative test name
2. Offline: `--filter-tag 'integration & stable'`, or the `release.txt` preset,
   which also drops `bench`
3. After a fix: `--filter-continue <file>`, see [README.md](README.md)

## Golden Files and Hashes

Goldens are `*.N.stdout` next to the case: created on the first run, a mismatch
**fails** afterwards, so delete the sidecar to regenerate. `stable_hash: true`
on a step, the default, likewise compares the leader's execution hash against a
`.N.hash` sidecar; with `false` there is no sidecar and validators compare
against the leader's runtime hash

`--ignore-hash` skips both comparison and sidecar creation — needed in runner
dev mode, where every execution hash moves
([modify-runner.md](../extending/modify-runner.md)), not for a new case. A line
can stop tracking sidecars altogether by returning `{'save-hashes': False}` from
`integration()` in its `.genvm-tool.py`, as v0.3.x currently does while its
hashes still move

Neither switch turns off the leader-vs-validator/sync comparison: with sidecars
disabled a non-main mode is compared against the main mode's hash from the same
run instead. Only that comparison makes a determinism regression visible, and a
non-main step has no semantics goldens of its own, so without it the step would
assert nothing beyond "did not crash"

## Debug Mode

Cases run at `unsafe`, where `py-genlayer:test` resolves and wall-clock is
exposed. A top-level `debug_mode` in the jsonnet lowers that to `safe`,
`safe-unbounded` or raises it to `unsafe-tracing`; `disabled` is rejected,
because `reroute_to` — what points a case at its own line's executor — is
honored only from `safe` up. Below `unsafe` a contract must name its runner by
hash instead of `:test`. Write `@RUNNER_LATEST_<line>_<runner>@` for that hash,
for example `py-genlayer:@RUNNER_LATEST_v0.3_py-genlayer@`: the harness replaces
it with the uid in `build/out/executor/<version>/data/latest.json` before the
code reaches an executor, so a runner change needs no test edit. A literal uid
still works and goes stale

## Services

The manager, the modules and the webdriver start automatically; `--no-manager`
and `--no-webdriver` reuse externally running ones (a manual webdriver is
`bash webdriver/build-and-run.sh`). After WASM files change,
`./build/out/executor/<version>/bin/genvm precompile` saves test time

`--no-manager` omits cases that require a manager with a suite-owned config or
restart lifecycle
