# Adding a WASI Function

Paths are relative to `executors/<line>.x/`

## Through gl_call — Preferred

A new gl_call method is a minor-compatible ABI addition, see the
[versioning spec](../../../website/src/spec/01-core-architecture/03-versioning.rst)

1. `executor/crates/sdk-rs/src/abi/gl_call.rs` — add the message shape
2. `executor/src/wasi/genlayer_sdk/` — implement the handler, with a version
   check so an older contract does not observe the new method

## Raw WASI Function

This changes the ABI surface itself

1. `executor/src/wasi/witx/genlayer_sdk.witx` — add the declaration
2. `executor/src/wasi/genlayer_sdk/` — implement it under the generated trait
3. `runners/cpython/modules/_genlayer_wasi/genlayer.c` — add the Python proxy.
   This changes the cpython runner hash, so refresh it per
   [modify-runner.md](modify-runner.md)
