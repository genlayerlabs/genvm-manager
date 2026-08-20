VM Execution Result
===================

.. _gvm-def-vm-result:

Result Kinds
------------

.. _gvm-def-return:

.. rubric:: Return

Represents successful execution of a :term:`sub-VM`

.. _gvm-def-vm-error:

.. rubric:: VMError

Represents a VM produced error, such as non-zero exit code or
exceeding resource limits.

It uses predefined string error codes.

.. _gvm-def-fatal-vm-error:

.. rubric:: Fatal VM Error

Every VM error is either **fatal** or not; both draw their code from the same
set. A non-fatal error of a :term:`sub-VM` is returned to its caller as
:ref:`gvm-def-subvm-result-encoding`. A fatal one is not catchable: the caller
terminates with the same VM error, and propagation continues until the topmost
VM boundary

Nested transport encodes a fatal VM error with result code ``4``. At the
topmost publication boundary, the executor MUST downgrade it to an ordinary
:ref:`gvm-def-vm-error` with the same payload before producing the reported
result. Result code ``4`` is therefore forbidden in a top-level reported
result. Both its :ref:`gvm-def-execution-hash` and its
:ref:`gvm-def-subvm-hash` use ``VMError`` as the result kind

.. _gvm-def-vm-error-code:

VM Error Code Format
--------------------

::

   vm-error-code := public-code [ " # " detail ]

- ``public-code`` — a sequence of ``snake_case`` components separated by single
  spaces, drawn from the :ref:`predefined codes <gvm-def-str-trie-vm-error>`. It
  never contains ``#``.
- ``detail`` — optional free-form UTF-8 diagnostic. When present, it is
  separated from the public code by ``#`` surrounded by a single space on each
  side.

The full string, detail included, is covered by the
:ref:`gvm-def-execution-hash`, so it must be reproducible octet for octet by
every implementation running the same execution. A detail MUST therefore be
composed only of:

#. octets the execution itself produced or consumed — a guest-supplied string,
   a decoded field of the calldata or of a runner description;
#. values fixed by the WASM module or by this specification — a function or
   memory index, a limit, a constant from :doc:`../appendix/constants`;
#. literal text chosen by the code that raises the error.

Anything the host or the runtime environment supplies MUST NOT appear, even
indirectly. In particular: filesystem paths, operating-system error strings or
numbers, host addresses and pointer values, elapsed times, locale-dependent or
platform-width-dependent formatting, thread or process identifiers, and
implementation build or version strings.

Beyond that constraint the detail's content carries no compatibility promise —
see :ref:`gvm-def-vm-error-compat`.

Consumers MUST compare and match VM error codes only by the public code (the
part before the first `` # ``). A consumer matching a known code ``P`` MUST
treat a code as matching iff its public code equals ``P`` or extends ``P``
with further space-separated components.

.. _gvm-def-user-error:

.. rubric:: UserError

Represents a user-produced error in utf-8 format.

.. rubric:: Effects of a Non-Returning Run

Only a :ref:`gvm-def-return` carries effects. A topmost run that ends in
:ref:`gvm-def-user-error` or :ref:`gvm-def-vm-error` reports no
``storage_changes`` and no ``emissions``, whatever it wrote or emitted before
failing, and its :ref:`gvm-def-execution-hash` covers those empty fields.

.. _gvm-def-internal-error:

InternalError
-------------

Not a :ref:`gvm-def-vm-result` but the absence of one: the executor could not
run the contract to a verdict at all, because of a :term:`Host` communication
failure or :term:`Module` unavailability.

Internal errors are not visible by the contracts and are reported only to the
:term:`Host`, which will most likely vote *timeout* if it encounters one

Non-Deterministic Block Result Encoding
---------------------------------------

- :ref:`gvm-def-return`\: Arbitrary structure in :ref:`gvm-def-calldata-encoding`
- :ref:`gvm-def-user-error`\: utf-8 string
- :ref:`gvm-def-vm-error`\: utf-8 string

These three are the only codes a leader-proposed non-deterministic block result
may carry; validators treat every other byte as a malformed leader result. A
fatal VM error computed by a leader's non-deterministic child propagates to its
caller and MUST NOT be encoded into ``nondet_results``

Contract Result Encoding
------------------------

:ref:`gvm-def-return`
~~~~~~~~~~~~~~~~~~~~~

Arbitrary structure in :ref:`gvm-def-calldata-encoding`

:ref:`gvm-def-user-error` and :ref:`gvm-def-vm-error`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The error value (``UserError`` value or ``VMError`` error code string) is reported
alongside two fingerprint fields, each :ref:`gvm-def-calldata-encoding` encoded:

``backtrace`` — the WASM call stack captured at the failure point:

.. code-block:: json

  [
    {
      "module_name": "<module_name>",
      "func": "<number: function_index>"
    }
  ]

``wasm_store_hashes`` — per-module memory fingerprint:

.. code-block:: json

  {
    "<module_name>": {
      "memories": [
        "<bytes: 32_byte_blake3_hash>"
      ]
    }
  }

For sake of preventing skipping execution for error results, validators are obligated to calculate
the VM fingerprint on error.

Both fields are serialized using :ref:`gvm-def-calldata-encoding` to be deterministic, and have the following structure:

#. ``backtrace`` frames are ordered from most recent to oldest one (most likely, ``_start``)
#. Function index is an index of function in WASM module
#. Memories are ordered by their index in WASM module
#. Memories are hashed using BLAKE3 hash function, which is cryptographically secure and provides acceptable performance

.. _gvm-def-execution-hash:

Execution Hash
--------------

Every run produces an *execution hash*: a SHA3-256 digest over a
:ref:`gvm-def-calldata-encoding` encoding of the consensus-visible result, as a map
with the following keys (in this order):

#. ``backtrace``
#. ``data`` — the contract result value
#. ``data_fees_consumed``
#. ``data_fees_remaining``
#. ``emissions`` — emitted messages and events, in emission order
#. ``kind`` — the :ref:`gvm-def-vm-result` result code
#. ``storage_changes``
#. ``subvm_hashes`` — see :ref:`gvm-def-subvm-hash`
#. ``wasm_store_hashes``

Two runs that agree on the deterministic result produce the same execution hash, so
consensus can compare a single 32-byte value instead of the full result.

A fatal VM error is committed with ``VMError`` as ``kind``. Fatality controls
propagation and is not a distinct consensus-visible outcome

``emissions`` covers the whole content of every emitted message and event, not
just its metered cost: two emissions can carry different calldata or different
event topics for the same fee, so a fee-only commitment would let nodes agree on
the hash while committing divergent side effects

.. _gvm-def-subvm-hash:

Sub-VM Result Hash
------------------

A deterministic run accumulates the result of each deterministic :term:`sub-VM` call
into a rolling SHA3-256 accumulator. The finalized accumulator is the
``subvm_hashes`` field of the parent's :ref:`gvm-def-execution-hash`.

For each deterministic sub-VM, its *small hash* is folded into the accumulator. The
small hash is a SHA3-256 digest over a :ref:`gvm-def-calldata-encoding` encoded map:

#. ``kind`` — the exact string ``"Return"``, ``"UserError"`` or ``"VMError"``.
   Fatality is not an outcome of its own, only a statement about who may catch
   it, so a fatal result hashes as ``"VMError"``
#. ``result`` — for ``Return``/``UserError`` the result value in
   :ref:`gvm-def-calldata-encoding`; for ``VMError`` the error code string
#. ``subvm_hashes`` — that sub-VM's own finalized accumulator, making the hash recursive over the whole deterministic call tree
#. ``wasm_store_hashes``

Non-deterministic sub-calls do not contribute. A run with no deterministic sub-calls
finalizes its accumulator to a fixed digest, so that error and edge results hash
uniformly.

The small hash of a sub-VM the host delegated to another executor
(see :ref:`gvm-def-contract-version`) is computed the same way, from the values
that executor reports. Where the callee ran is not an input: a call that a host
routes to another executor MUST fold the same value it would have folded had the
callee run in-process.

Post-Execution Result Validation
--------------------------------

A consumer in :ref:`gvm-def-sync-mode` or :ref:`gvm-def-validator-mode`
consumes one leader-proposed result per non-deterministic block it runs. If the
leader supplied **more** results than the run consumed, the surplus blocks were
never reached: the run's result is replaced by
:ref:`gvm-def-str-trie-value-vm-error-leader-fault-nondet-output-extra`, whose
parameter is the first 6 characters of the :ref:`gvm-def-gvm32` encoding of the
``sha3_256`` digest of the complete ``[result_code][data]``
:ref:`sub-VM result buffer <gvm-def-subvm-result-encoding>` the run would
otherwise have returned — the whole wire buffer as emitted, not a decoded
payload or alternate representation. No non-deterministic disagreement is caused
by this.
