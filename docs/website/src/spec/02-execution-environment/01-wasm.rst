WASM Utilization
================

Enabled WASM Features and Proposals
-----------------------------------

#. Core Modules
#. Bulk Memory
#. Sign Extension
#. Mutable Globals
#. Multi Value
#. SIMD
#. Saturating Float to Int Conversions
#. Tail Call

:ref:`gvm-def-det-mode` Additional Limitations
----------------------------------------------

Only following ``f32.*`` and ``f64.*`` operations are allowed:

- ``f32.store``, ``f64.store``
- ``f32.load``, ``f64.load``
- ``f32.const``, ``f64.const``
- ``f32.reinterpret_i32``, ``f64.reinterpret_i64``
- ``i32.reinterpret_f32``, ``i64.reinterpret_f64``

Any other floating point operation is considered non-deterministic and is not allowed in :ref:`gvm-def-det-mode`.

:ref:`gvm-def-non-det-mode` does not have these limitations, allowing all floating point operations.

Stack Limiting
--------------

WASM recursion depth is bounded by two counters, each private to a
:term:`sub-VM`, so the depth at which execution traps depends only on the WASM
code and its dynamic call pattern — never on how the code was compiled:

- **call stack**: starts at :ref:`gvm-def-consts-value-wasm-stack-limits-call-depth`;
  every active frame costs :math:`1`.
- **value stack**: starts at :ref:`gvm-def-consts-value-wasm-stack-limits-value-slots`;
  every active frame costs its function's *value size* — the number of declared
  locals plus the maximum operand-stack depth of the function body. The value
  size is derived statically from the function's code; every value counts as
  one slot regardless of its type. Function parameters are not part of the
  callee's value size: for a plain ``call`` they are accounted in the
  *caller*'s operand-stack depth (see the tail-call case below).

The counters change only at frame boundaries:

#. ``call`` (also ``call_indirect``, and the initial host entry into the
   :term:`sub-VM`): on entry the *callee*
   charges both counters — the call stack by :math:`1`, the value stack by its
   value size. If either counter would go below zero, execution traps with
   :ref:`gvm-def-str-trie-value-vm-error-wasm-trap-stack-overflow`. The
   *caller*'s charges remain held for the duration of the call.
#. ``return`` (including falling off the end of the body): the *callee* refunds
   exactly what it charged, immediately before control transfers back to the
   *caller*.
#. ``return_call`` (also ``return_call_indirect``): the *caller* refunds its
   own charges **before** transferring control, then the *callee* charges as
   under ``call`` — a tail-call chain therefore executes at constant depth.
   The tail call's arguments are released by the *caller*'s refund and are
   covered by no charge while the *callee* runs; the same holds for the
   parameters of the initial host entry, which has no WASM *caller*.

Calls into host functions charge nothing. Unwinding refunds nothing: whatever
terminates execution early — a trap or a host-initiated exit — makes the
:term:`sub-VM` exit, discarding its counters.

An implementation may keep a native-stack safety backstop, but it must be
sized so that it can never trip before these counters do.

A function whose value size alone exceeds
:ref:`gvm-def-consts-value-wasm-stack-limits-value-slots` is rejected when the
module is compiled, with
:ref:`gvm-def-str-trie-value-vm-error-invalid-contract-wasm-validating`.

RAM Consumption
---------------

Each WASM table element imposes :ref:`gvm-def-consts-value-memory-limiter-consts-table-entry` :ref:`gvm-def-ram-consumption`\.

Each WASM Memory costs length of bytes it has. A runtime WASM ``memory.grow`` instruction which would exceed the limit returns :math:`-1` and leaves memory unchanged. An instantiation-time reservation that cannot be met is fatal instead; see :ref:`gvm-def-ram-consumption` for the exact error.
