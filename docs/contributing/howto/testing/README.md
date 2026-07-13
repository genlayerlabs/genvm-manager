# Test

All tests run through `genvm-tool test`. Build first
([build.md](../building/build.md)). Commands assume the `#full` dev shell.

Per-kind guides: [rust.md](rust.md) · [python.md](python.md) ·
[integration.md](integration.md) · [fuzzing.md](fuzzing.md).

## Quick reference

| What | Command |
|---|---|
| All tests | `genvm-tool test run` |
| Release preset | `genvm-tool test run --filter-tag "$(cat tests/presets/release.txt)"` |
| One test | `genvm-tool test run --filter-name 'test_name'` |
| Re-run failed | `genvm-tool test run --filter-continue <file>` |
| List tests / plan / tags / services | `genvm-tool test show test\|plan\|tags\|services` |

Options: `--filter-name REGEX`, `--filter-tag EXPR` (e.g. `stable & !slow`),
`--filter-continue FILE`, `--fail-fast`, `--coverage`,
`--log-level {trace,debug,info,warning,error}`, `--ignore-hash`.

Presets in `tests/presets/`: `release.txt` (`integration & stable & !bench`),
`rust.txt` (`(rust | integration) & !bench & !fuzz`), `rust-fuzz.txt`
(`rust & fuzz`), `python.txt` (`python`).

Common tags are documented in [integration.md](integration.md);
`genvm-tool test show tags` lists everything.

## Fix–rerun loop: use continue files

Every failed run writes the failing test names to
`build/test-artifacts/continue/<timestamp>-<random>` (the name is printed in
the failure summary). **After fixing something, rerun just those tests first**
— much faster than a full rerun:

```bash
genvm-tool test run --filter-continue <name-or-path>
```

Do a full run only once the continue set passes.

## Configuration

Test-suite configuration (artifacts dir, python paths) comes from
`.genvm-monorepo-root`; the suite itself is the `tests` function in
`.genvm-tool.py`. Test definitions live in `tests/system/<name>/test.py` —
see [genvm-tool.md](../genvm-tool.md).
