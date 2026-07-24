# Rust tests

## Where to put tests

1. If it is trivial, put it as an integration test into `<crate-dir>/tests/*.rs`
2. If the entire `mod tests` is below 30 lines, you can put it inline
3. Tests for `mod.rs` go in a `tests.rs` alongside it, declared with `mod tests;`
4. Tests for `abc.rs` go in `abc_test.rs`, referenced with `#[path = "abc_test.rs"] mod tests;`

## How to run

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
