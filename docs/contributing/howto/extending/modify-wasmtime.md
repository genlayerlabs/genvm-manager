# Modifying vendored wasmtime

The tree at `executors/<line>.x/executor/third-party/wasmtime` is a
git-third-party clone (upstream pin + genvm patches) — mechanism and commit
workflow: [git-third-party.md](../committing/git-third-party.md).

Edit the tree directly; `cargo check`/ninja build it in-tree, so iteration is
normal Rust development. Two caveats:

- **Nix builds don't see working-tree edits** — they apply the saved patches
  (`support/nix/git-third-party.nix`). Run `git third-party save` before any
  nix build/release must pick the change up.
- Commit flow: commit inside the nested wasmtime repo →
  `git third-party save executor/third-party/wasmtime` → commit
  `.git-third-party/` in the executor repo → gitlink bump in the manager.

## genvm patch points

- Custom trap variant `Trap::NondetInstruction`
  (`crates/environ/src/trap_encoding.rs`) — raised for instructions disallowed
  in deterministic mode (e.g. unmapped float/SIMD ops); emitted via
  `trap_nondet_instruction` (`crates/cranelift/src/func_environ.rs`) from
  `float_op_unreachable_check` (`crates/cranelift/src/translate/code_translator.rs`).
- When adding a `Trap` variant: update the `check!` macro in `from_u8` and its
  `Display` arm (see the comment at the enum), the `TRAP_*` const in
  `crates/cranelift/src/lib.rs`, and the executor-side remap to a public VM
  error code in `executor/src/rt/errors.rs` (`trap_to_vm_error`).
