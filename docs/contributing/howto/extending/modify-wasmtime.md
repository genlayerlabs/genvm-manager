# Modifying Vendored Wasmtime

`executors/<line>.x/executor/third-party/wasmtime` is a git-third-party clone:
an upstream pin plus genvm patches. Mechanism and commit flow:
[git-third-party.md](../committing/git-third-party.md)

Edit the tree directly — `cargo check` and ninja build it in place, so iteration
is normal Rust development. Nix builds do **not** see working-tree edits, they
apply the saved patches, so run `git third-party save` before any nix build or
release must pick a change up

## genvm Patch Points

1. The custom trap `Trap::NondetInstruction`
   (`crates/environ/src/trap_encoding.rs`), raised for instructions disallowed
   in deterministic mode, such as unmapped float and SIMD ops. It is emitted
   through `trap_nondet_instruction`
   (`crates/cranelift/src/func_environ.rs`) from `float_op_unreachable_check`
   (`crates/cranelift/src/translate/code_translator.rs`)
2. A new `Trap` variant needs: the `check!` macro in `from_u8` and its `Display`
   arm, the `TRAP_*` const in `crates/cranelift/src/lib.rs`, and the
   executor-side remap in `executor/src/rt/errors.rs` (`trap_to_vm_error`)
