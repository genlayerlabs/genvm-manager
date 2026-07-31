# Rust Tests

Collection scans every tracked `Cargo.toml` in the umbrella, so manager crates,
shared `crates/` and executor crates are all picked up

## Where To Put a Test

1. If it is trivial, make it an integration test in `<crate-dir>/tests/*.rs`
2. If the whole `mod tests` stays below 30 lines, inline it
3. Tests for `mod.rs` go into a sibling `tests.rs`, declared as `mod tests;`
4. Tests for `abc.rs` go into `abc_test.rs`, referenced with
   `#[path = "abc_test.rs"] mod tests;`

## How To Run

```bash
genvm-tool test run --filter-tag rust            # --coverage for coverage
```

The `rust.txt` preset is `(rust | integration) & !bench & !fuzz`

If a test fails to load a shared library under the `#full` shell, use the
dedicated one:

```bash
nix develop '.?submodules=1#rust-test' --command genvm-tool test run --filter-tag rust
```
