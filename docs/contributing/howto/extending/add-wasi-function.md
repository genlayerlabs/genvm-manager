# Adding a WASI function to an executor

Paths are relative to `executors/<line>.x/`.

## Via gl_call (preferred — no ABI surface change)

1. `executor/crates/sdk-rs/src/abi/gl_call.rs` — add the message shape.
2. `executor/src/wasi/genlayer_sdk.rs` — implement the handler; add a version
   check so older contracts don't observe the new method.

New gl_call methods are a minor-compatible ABI addition — see the
[versioning spec](../../../website/src/spec/01-core-architecture/03-versioning.rst).

## Raw WASI function (new ABI surface)

1. `executor/src/wasi/witx/genlayer_sdk.witx` — add the declaration.
2. `executor/src/wasi/genlayer_sdk.rs` — add the implementation (under the
   generated `impl` trait).
3. `runners/cpython/modules/_genlayer_wasi/genlayer.c` — add the Python proxy.
   This changes the cpython runner hash — refresh it per
   [modify-runner.md](modify-runner.md).
