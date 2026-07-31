# Testing

Everything runs through `genvm-tool test`, from the `#full` dev shell, after a
build ([build.md](../building/build.md)). Per-kind: [rust.md](rust.md) ·
[python.md](python.md) · [integration.md](integration.md) ·
[fuzzing.md](fuzzing.md)

| What | Command |
|---|---|
| All tests | `genvm-tool test run` |
| Preset | `genvm-tool test run --filter-tag "$(cat tests/presets/release.txt)"` |
| One test | `genvm-tool test run --filter-name 'test_name'` |
| Re-run failed | `genvm-tool test run --filter-continue <file>` |
| Inspect | `genvm-tool test show test\|plan\|tags\|services` |

`run` options: `--filter-name REGEX`, `--filter-tag EXPR`,
`--filter-continue FILE`, `--fail-fast`, `--max-concurrent N`, `--coverage`,
`--ignore-hash`, `--junit-xml`. `--log-level` is global, so it goes before the
subcommand

Presets in `tests/presets/`: `release.txt` is `integration & stable & !bench`,
`rust.txt` is `(rust | integration) & !bench & !fuzz`, `rust-fuzz.txt` is
`rust & fuzz`, `python.txt` is `python`. `genvm-tool test show tags` lists every
tag, the integration ones are in [integration.md](integration.md)

## Fix–Rerun Loop

A failed run writes the failing names to
`build/test-artifacts/continue/<timestamp>-<random>` and prints that name in the
summary. After a fix rerun only those, then do a full run once they pass:

```bash
genvm-tool test run --filter-continue <name-or-path>
```

## Where Tests Come From

Rust from every tracked `Cargo.toml` and its `fuzz/*.rs`, python from
`executors/v0.3.x/runners/genlayer-py-std`, integration from each line's
`tests/integration/`, system from `tests/system/<name>/test.py`. Collection is
wired in the root `.genvm-tool.py`, runner configuration in
`.genvm-monorepo-root` ([genvm-tool.md](../genvm-tool.md))
