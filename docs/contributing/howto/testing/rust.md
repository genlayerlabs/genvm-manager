# Rust tests

```bash
genvm-tool test run --filter-tag rust            # add --coverage for coverage
```

If a Rust test fails to load a shared library under the `#full` shell, use the
dedicated shell:

```bash
nix develop '.?submodules=1#rust-test' --command genvm-tool test run --filter-tag rust
```

Tests live in the executor crates (inline `#[cfg(test)] mod tests` and each
crate's `tests/`); `genvm-tool test` discovers them. Preset:
`tests/presets/rust.txt` (`rust | integration`).
