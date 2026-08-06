Contract Upgradability
======================

:term:`GenVM` provides a native contract upgradability system that allows contracts to be modified after deployment
while maintaining security guarantees and clear access controls.

Data that is necessary for this process resides in :ref:`genvm-def-root-slot`\.

Upgrade Control Mechanism
-------------------------

The upgrade system works through access control during write transactions:

#. At start of execution :term:`GenVM` reads the ``upgraders`` list of :ref:`genvm-def-root-slot`\.
    It does not lead to :ref:`gvm-def-ram-consumption`
#. If the sender is not in the ``upgraders`` list of :ref:`genvm-def-root-slot`\,
    :term:`GenVM` reads ``locked_slots`` and will prevent writing to them.
    It implies :math:`32*n` :ref:`gvm-def-ram-consumption`\, where :math:`n` is the number of locked slots
    (bounded by :ref:`gvm-def-consts-value-top-limits-locked-slots`).
    This memory is never released.
#. :term:`GenVM` loads the contract code as the entry runner. This is a runner
    load action; its :ref:`gvm-def-ram-consumption` is specified in
    :doc:`../02-execution-environment/04-runners` and released when the
    :term:`sub-VM` finishes.

.. _gvm-def-locked-slot-nesting:

Locked Slots in Nested Calls
----------------------------

The ``upgraders`` and ``locked_slots`` lists are read once per execution, for
the top-level contract and the top-level sender, before any :term:`sub-VM`
exists; the resulting charge is the one in `Upgrade Control Mechanism`_ and no
:term:`sub-VM` pays it again.

One set suffices because only the top-level contract's storage is ever
writable within an execution:

#. A :ref:`gvm-def-gl-call-call-contract` child cannot write storage at all
   (see :ref:`gvm-meta-property-derivation`), so the callee's own
   ``locked_slots`` are never consulted, and being an upgrader of the callee
   grants a calling contract nothing.
#. A :ref:`gvm-def-gl-call-sandbox` child that was granted
   :ref:`gvm-perm-write-storage` writes the *same* contract's storage as its
   parent, under the same sender — the top-level set applies to it unchanged.
#. A :ref:`gvm-def-gl-call-run-nondet` child cannot write storage.

.. _gvm-def-contract-version:

Contract Major Version
----------------------

The ``major`` field of :ref:`genvm-def-root-slot` is at
:ref:`gvm-def-consts-value-root-offsets-major` and stores the public-ABI major
version that the contract was built against. Top-level and runner loads compare
this byte to ``CURRENT_MAJOR`` and fail with
:ref:`gvm-def-str-trie-value-vm-error-invalid-contract-major-mismatch` when it
differs. :ref:`gvm-def-gl-call-call-contract` may instead delegate the callee to
another executor selected by the host.

The value is **not** modifiable by the contract itself: it is written once at deploy time by
the host from a value detected in the contract package (a ``genvm.version`` custom WASM
section, a ``version`` file inside a zip-packaged runner, or a leading ``// vX.Y.Z`` comment
in single-file text contracts). The host detection flow and the on-the-wire endpoint
(``POST /contract/detect-version``) are described in the impl-spec.

Implications for upgrades:

- Replacing ``code`` with a binary of a different public-ABI major leaves ``major``
  stale; the contract will fail to load on subsequent invocations. Upgraders MUST update
  ``major`` in the same transaction when crossing a major boundary.
- Bumping ``major`` to a value the running :term:`GenVM` does not support produces a
  load-time error; the upgrade transaction must be performed on a host that already supports
  the target major.
