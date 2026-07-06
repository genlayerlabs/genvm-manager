Resource Limiting
=================

:ref:`gvm-def-det-mode` and :ref:`gvm-def-non-det-mode` have two separate RAM budgets.
Each budget starts at 4294967295 octets (4 GiB).
All :term:`sub-VM` instances within the same mode share the same budget.

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
  costs a flat load constant plus the runner's size in octets. A runner already
  in that :term:`sub-VM`'s loaded set costs nothing, and the charge is released
  when the :term:`sub-VM` finishes, like any other charge. Loading covers
  spawning the entry-point runner, ``Depends``/``With`` actions, the ``MapFile``
  and ``RegisterRunner`` ``gl_call``\ s, and inheriting a custom runner at
  sub-VM creation (see :doc:`../02-execution-environment/04-runners`)

The load constant is a fixed per-load overhead. Its value currently lives in
the executor implementation, pending migration to the public ABI; once
migrated it will be published in :doc:`../appendix/constants` alongside the
other memory-limiter constants. Its value is **pending**.

RAM Release
-----------

File content memory is released when the corresponding file descriptor is closed via ``fd_close``.
When a :term:`sub-VM` finishes execution, all remaining RAM consumed by it is released back to the shared budget.
This applies to runner charges as well: memory consumed by loading or
registering a runner is released when the registering :term:`sub-VM` finishes,
like any other charge. There are no permanent charges.

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
