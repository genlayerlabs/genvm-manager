# Integration tests

```bash
genvm-tool test run --filter-tag integration
```

Cases are `.jsonnet` files in `executors/<line>.x/tests/integration/`; each
produces a `<name>/prepare` case plus one case per step (`/0l`, `/0v`, `/0s`, …).
A sibling `.skip` file marks a case skipped. The suite iterates every line in
`active-versions` (`.genvm-monorepo-root`) and runs each line's cases against
that line's own built executor (`build/info.json` maps `v0.3` → the concrete
built version).

## Common tags

| Tag | Meaning |
|---|---|
| `stable` | needs **no LLM keys and no webdriver** (manager only) — safe to run offline |
| `unstable`, `semi-stable` | need modules + webdriver; `unstable` is retried (3 attempts), `semi-stable` is not |
| `bench` | benchmarks — excluded from the presets |

## Running a subset

- **One executor line** — test names are repo-relative paths and
  `--filter-name` is an unanchored regex, so:
  `genvm-tool test run --filter-name 'executors/v0.3.x/'`
- **Offline / no LLMs / no web** — the `stable` tag marks tests needing only
  the manager (no modules, no webdriver; the executor is told `no_modules`):
  `--filter-tag 'integration & stable'` (`tests/presets/release.txt`
  additionally excludes `bench`). Stability is the second argument of
  `util.features(paths, stability)` in the case's jsonnet `tags`
  (`'stable'` / `'semi-stable'` / `'unstable'`). (Legacy: v0.2.x instead uses a
  `stable/` top-level directory.)
- **After a fix** — rerun only the failed cases via the continue file:
  `--filter-continue <file>` (see [README.md](README.md)).

## Golden files and hashes

Goldens live next to the cases (`*.N.stdout`). A missing golden is
auto-created on the first run; after that a mismatch **fails** the test —
delete the sidecar to regenerate it.

With `stable_hash: true` on a step entry (the default) the leader's execution
hash is likewise compared against an auto-created `.N.hash` sidecar; with
`stable_hash: false` there is no sidecar and validators compare to the
leader's runtime hash instead.

`--ignore-hash` skips hash comparison *and* sidecar creation (a new case does
NOT need it — its hash is auto-created). Use it when hashes legitimately
differ, e.g. **runner dev mode**
([modify-runner.md](../extending/modify-runner.md)): a modified runner changes
runner hashes and therefore every execution hash. An executor line can also
disable hash checks wholesale via `integration()` in its `.genvm-tool.py`
returning `{'ignore-hash': True}` — currently the case for v0.3.x.

## Services

The manager, modules (LLM/web), and webdriver services start automatically;
`--no-manager` / `--no-webdriver` use externally running ones instead. Manual
webdriver: `bash webdriver/build-and-run.sh`. If WASM files changed,
`./build/out/executor/<version>/bin/genvm precompile` saves test time.
