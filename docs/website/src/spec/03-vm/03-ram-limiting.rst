Resource Limiting
=================

:ref:`gvm-def-det-mode` and :ref:`gvm-def-non-det-mode` have separate RAM
budgets: what one consumes is never charged to the other.
The deterministic budget starts at 4294967295 octets (4 GiB).
Every :ref:`gvm-def-gl-call-run-nondet` gets its own non-deterministic budget,
starting at what its caller had remaining at the moment of the call, so a
nondet block never gets more RAM than its caller had left.
All :term:`sub-VM` instances within one budget share it.

.. _gvm-def-ram-consumption:

RAM Consumption
---------------

Every resource allocation subtracts from the RAM budget of the current :ref:`gvm-def-vm-mode`.
When an allocation would cause the remaining budget to become negative,
the :term:`sub-VM` exits with :ref:`gvm-def-vm-error` carrying the
:ref:`gvm-def-str-trie-value-vm-error-out-of-memory` message, or one of its
:ref:`gvm-def-str-trie-value-vm-error-out-of-memory-wasm-memory` /
:ref:`gvm-def-str-trie-value-vm-error-out-of-memory-wasm-table` variants for the
corresponding WASM allocations. ``memory.grow`` and ``table.grow`` are the
exception: at runtime they are recoverable rather than fatal (see the two
bullets below).

The following operations consume RAM:

- **WASM memory growth**: each page (65536 octets) costs its size in bytes.
  A runtime ``memory.grow`` that would exceed the budget is **not** fatal:
  following the WASM specification it leaves memory unchanged and evaluates to
  :math:`-1`, so the guest can react. Only the memory's initial,
  instantiation-time reservation being unmet makes the :term:`sub-VM` exit with
  :ref:`gvm-def-str-trie-value-vm-error-out-of-memory-wasm-memory`.
- **WASM table growth**: each table entry costs :ref:`gvm-def-consts-value-memory-limiter-consts-table-entry` octets.
  As with memory, a runtime ``table.grow`` beyond the budget evaluates to
  :math:`-1`; only the instantiation-time reservation being unmet exits with
  :ref:`gvm-def-str-trie-value-vm-error-out-of-memory-wasm-table`.
- **File mapping**: :ref:`gvm-def-consts-value-memory-limiter-consts-file-mapping` octets base cost plus the length of the filename in bytes
- **File descriptor allocation**: :ref:`gvm-def-consts-value-memory-limiter-consts-fd-allocation` octets per descriptor
- **Runner loading**: the first load of a :term:`runner` in a :term:`sub-VM`
  costs :ref:`gvm-def-consts-value-memory-limiter-consts-runner-load-cost` plus the runner's size in octets. A runner already
  in that :term:`sub-VM`'s loaded set costs nothing, and the charge is released
  when the :term:`sub-VM` finishes, like any other charge. Loading covers
  spawning the entry-point runner, ``Depends``/``With`` actions, the ``MapFile``
  and ``RegisterRunner`` ``gl_call``\ s, and receiving a custom-runner grant at
  sub-VM creation (see :doc:`../02-execution-environment/04-runners` and
  :ref:`gvm-meta-property-custom-runners`)
- **Storage writes**: writing to a 32-octet aligned region of a
  :term:`Storage Slot` costs
  :ref:`gvm-def-consts-value-memory-limiter-consts-new-storage-page` octets the
  first time that region is written. Regions the :term:`sub-VM` inherited
  already written from its caller, and repeated writes to a region, cost nothing
- **Emissions**: each emitted message or event costs
  :ref:`gvm-def-consts-value-memory-limiter-consts-execution-emission-base-size`
  octets,
  plus its retained calldata, code, allocation subtree, topics, event data, and
  :ref:`gvm-def-consts-value-memory-limiter-consts-calldata-arg-element-size`
  octets per
  retained positional argument,
  :ref:`gvm-def-consts-value-memory-limiter-consts-calldata-kwarg-entry-size`
  octets per
  retained keyword argument, and
  :ref:`gvm-def-consts-value-memory-limiter-consts-message-fee-rotation-element-size`
  octets
  per retained message-fee rotation
- **Nondeterministic outputs**: each output costs
  :ref:`gvm-def-consts-value-memory-limiter-consts-nondet-output-base-size`
  octets plus its encoded length on every role
- **Sub-VM creation**: each new :term:`sub-VM` costs
  :ref:`gvm-def-consts-value-memory-limiter-consts-vm-spawn-cost` octets, plus
  :ref:`gvm-def-consts-value-memory-limiter-consts-storage-page-inherited`
  octets for every 32-octet region already written in the storage it inherits.
  Both are charged to the new :term:`sub-VM` at creation (see :doc:`01-startup`)
  and released when it finishes

The runner load cost (:ref:`gvm-def-consts-value-memory-limiter-consts-runner-load-cost`) is a fixed per-load
overhead

.. _gvm-def-nondeterministic-output-caps:

Nondeterministic Output Caps
----------------------------

Before entering a non-deterministic :term:`sub-VM`, the caller must have enough
RAM to retain the canonical memory-limit and non-deterministic-output fee-limit
results and return either as a file descriptor. It also checks whether both
results can be charged against the non-deterministic-output fee; when either
cannot, the sub-VM is not entered

An output that cannot fit its non-deterministic-output fee charge is first
replaced with a
:ref:`gvm-def-str-trie-value-vm-error-out-of-receipt-nondet-output` result. If
that result, or an output within the fee limit, cannot fit its RAM charge, it is
replaced with a :ref:`gvm-def-str-trie-value-vm-error-out-of-memory` result. The
leader publishes the replacement and an honest replay charges the same encoded
result

A validator rejects a leader proposal that differs from its post-cap result as
a fatal :ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-malformed`
result without entering the validator sub-VM

RAM Release
-----------

File content memory is released when the corresponding file descriptor is closed via ``fd_close``.
When a :term:`sub-VM` finishes execution, all remaining RAM consumed by it is released back to the shared budget.
This applies to runner charges as well: memory consumed by loading or
registering a runner is released when the registering :term:`sub-VM` finishes,
like any other charge.

Charges for retained storage, emissions, and nondeterministic outputs are
permanent. When a :ref:`gvm-def-gl-call-sandbox` child returns and its caller
takes over the child's retained data, those charges are transferred to the
caller rather than released.

Other Limits
------------

In addition to the RAM budget, the following hard limits apply:

- Maximum :term:`sub-VM` nesting depth: :ref:`gvm-def-consts-value-top-limits-vm-recursion`
- Maximum ``RunNondet`` calls per execution: :ref:`gvm-def-consts-value-top-limits-nondet-blocks`
- Maximum open file descriptors per :term:`sub-VM`: :ref:`gvm-def-consts-value-top-limits-max-fds`
- Maximum locked storage slots: :ref:`gvm-def-consts-value-top-limits-locked-slots`
- Minimum remaining RAM to issue a ``WebRequest``: :ref:`gvm-def-consts-value-top-limits-web-request-min-space` octets
- Minimum remaining RAM to issue a ``WebRender``: :ref:`gvm-def-consts-value-top-limits-web-render-min-space` octets

Exceeding any of these limits causes the :term:`sub-VM` to exit with :ref:`gvm-def-vm-error`.
