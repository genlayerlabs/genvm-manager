``gl_call`` Messages
====================

``EmitExternalMessage`` Message
-------------------------------

Sends transaction to Ethereum address with optional value transfer.

Payload
~~~~~~~

.. code-block::

   {
     "EmitExternalMessage": {
       "address": Address,      // 20-byte target address
       "calldata": Bytes,       // EVM calldata
       "value": U256            // Wei to transfer
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-send-messages`
#. Sufficient contract balance for value transfer

``ExternalCall`` Message
------------------------

Calls Ethereum contract method (read-only operation).

Payload
~~~~~~~

.. code-block::

   {
     "ExternalCall": {
       "address": Address,      // 20-byte target contract address
       "calldata": Bytes        // EVM calldata
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-call-others`

.. _gvm-def-gl-call-call-contract:

``CallContract`` Message
------------------------

Calls another GenLayer Intelligent Contract.

Payload
~~~~~~~

.. code-block::

   {
     "CallContract": {
       "address": Address,      // 20-byte target contract address
       "calldata": Calldata,    // Method call in calldata format
       "storage_view": Number,  // Storage view: 0=default, 1=latest_finalized, 2=latest_decided
       "catch_vm_error": Bool   // optional (default false): take a VM error as the result
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-call-others`
#. ``calldata`` satisfies :ref:`gvm-def-contract-call-conv`

Creates a :term:`sub-VM`. See :ref:`gvm-meta-property-derivation`.

A :term:`sub-VM` that ends in a :ref:`gvm-def-vm-error` normally ends its caller
too. With *param* ``catch_vm_error`` set, the caller reads that error as the
call's result instead. A fatal :ref:`gvm-def-vm-error` is never caught: the flag
does not apply to it, and the caller ends regardless.

Before every call, :term:`GenVM` asks the :term:`host` whether to delegate the
callee to another executor. A null answer preserves the local path, including
:ref:`gvm-def-str-trie-value-vm-error-invalid-contract-major-mismatch`. A
non-null answer executes the same derived :term:`sub-VM` in the selected
executor. The call result and its contribution to the execution hash are the
same kinds of observable output in either path. Routing cycles are permitted
and remain bounded by :ref:`gvm-def-consts-value-top-limits-vm-recursion`.

The calling convention is checked in the calling :term:`sub-VM`, before the
callee is spawned. A violation is the caller's own malformed argument, so it is
answered with ``Errno::Inval`` like the other argument checks and the caller can
recover — unlike a top-level entry, where the same violation is the execution's
result (:ref:`gvm-vm-startup-entry-validation`).

.. _gvm-def-emit-internal-message:

``EmitInternalMessage`` Message
-------------------------------

Posts message to GenLayer contract for later execution.

When GenVM forwards this message it derives :ref:`gvm-def-call-key` from the
``method`` field of ``calldata`` and attaches it to the emitted message.
:ref:`gvm-def-call-key` is the function-selector
analog used to identify the target method.

Payload
~~~~~~~

.. code-block::

   {
     "EmitInternalMessage": {
       "address": Address,      // 20-byte target contract address
       "calldata": Calldata,    // Method call in calldata format
       "value": U256,           // Wei to transfer
       "on": String,            // "finalized" or "decided"
       "use_balance": Bool,     // optional (default false), see below
       "fee_params": FeeParams  // optional (default absent), required iff use_balance
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-send-messages`
#. Sufficient contract balance for value transfer
#. When ``use_balance`` is set: :ref:`gvm-perm-use-balance-for-message-fees` and ``fee_params``
#. ``calldata`` satisfies :ref:`gvm-def-contract-call-conv`

.. _gvm-gl-call-balance-fees:

Balance-funded fees
~~~~~~~~~~~~~~~~~~~~

By default an outgoing internal message's fee is drawn from the sender's
prefunded message-fee pool and matched against the transaction's allocation
tree. Setting ``use_balance`` (the chain's ``useBalance``) instead funds the fee
from the **emitting contract's own balance**. ``fee_params`` carries the child
transaction's fee configuration and mirrors the chain's
``InternalMessageFeeParams``:

.. code-block::

   FeeParams {
     "leader_time_units_allocation": U256,     // per-round leader time units
     "validator_time_units_allocation": U256,  // per-round validator time units
     "execution_budget_per_round": U256,      // unified budget per leader round
     "rotations": [U256],                     // per-round rotations; non-empty.
                                              // rotations[0] is the initial round,
                                              // the rest are appeal rounds
                                              // (appealRounds = len - 1)
     "max_price_gen_per_time_unit": U256,     // GEN price cap; funding multiplier
     "storage_fee_max_gas_price": U256,       // storage price cap (revert guard)
     "receipt_fee_max_gas_price": U256        // receipt price cap (revert guard)
   }

Semantics:

- The fee is metered from ``fee_params`` and that metered amount becomes the child
  transaction's ``declaredBudget`` — the contract balance is the only bound. The
  consensus term is charged at the guest's ``max_price_gen_per_time_unit`` cap
  (matching the chain's ``minMessagePrimaryFees``), not the node's live
  ``genPerTimeUnit``, so the fee scales with the cap.
- The message is excluded from allocation matching, so no matching node is
  required (and none is consulted).
- The contract must be able to cover ``value + metered_fee`` from its balance;
  otherwise the call fails with ``InsufficientBalance``.
- The emitted allocation subtree is **empty**: nesting is fail-closed, so a child
  message must itself set ``use_balance`` or it fails to fund.

``fee_params`` is validated before metering. The following are rejected with
``Inval``:

- ``use_balance`` without ``fee_params``, or ``fee_params`` without ``use_balance``.
- Empty ``rotations`` (``appealRounds`` would underflow).
- A zero ``max_price_gen_per_time_unit``, ``storage_fee_max_gas_price`` or
  ``receipt_fee_max_gas_price`` (the chain reverts ``FeeValueMustBeNonZero`` at
  reveal).
- Out-of-bounds magnitudes: prices and budgets
  (``max_price_gen_per_time_unit``, ``storage_fee_max_gas_price``,
  ``receipt_fee_max_gas_price``, ``execution_budget_per_round``) must be below
  2\ :sup:`96`; counts (``leader_time_units_allocation``,
  ``validator_time_units_allocation``, each ``rotations`` entry) below
  2\ :sup:`32`. These bounds keep the metered floor within ``U256``.

Metering additionally enforces node-configured floors, surfaced as ``VMError``\ s:

- ``fee below_minimum`` — a per-phase time-unit allocation below
  ``node.minTimeUnitsPerPhase``, or a non-zero ``execution_budget_per_round`` below
  ``node.messageBudgetFloor`` (the chain's ``BudgetTooLow``).
- ``fee too_many_rounds`` — ``rotations`` implies more consensus rounds than the
  node's validator table supports (on-chain ``MAX_ROUNDS``).

``EmitInternalDeployMessage`` Message
------------------------------------

Deploys new intelligent contract to blockchain.

Payload
~~~~~~~

.. code-block::

   {
     "EmitInternalDeployMessage": {
       "calldata": Calldata,    // Constructor arguments in calldata format
       "code": Bytes,           // Contract bytecode
       "value": U256,           // Wei to transfer
       "on": String,            // "finalized" or "decided"
       "salt_nonce": U256,      // Salt for CREATE2-style deterministic addressing
       "use_balance": Bool,     // optional (default false)
       "fee_params": FeeParams  // optional (default absent), required iff use_balance
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-send-messages`
#. Sufficient contract balance for value transfer
#. When ``use_balance`` is set: :ref:`gvm-perm-use-balance-for-message-fees` and ``fee_params``
#. ``calldata`` satisfies :ref:`gvm-def-contract-call-conv`

Supports CREATE2-style deployment with salt nonce for deterministic addressing.
``use_balance`` / ``fee_params`` behave as for :ref:`gvm-gl-call-balance-fees`.

.. _gvm-def-gl-call-run-nondet:

``RunNondet`` Message
---------------------

Executes non-deterministic code with leader/validator consensus. See
:doc:`../../03-vm/04-determinism-mode-switching` and
:ref:`gvm-meta-property-derivation`.

Payload
~~~~~~~

.. code-block::

   {
     "RunNondet": {
       "data_leader": Bytes,       // Code/data for leader execution
       "data_validator": Bytes,    // Code/data for validator execution
       "runner": String,           // optional (default "contract"): runner to execute
       "custom_runners": [String], // optional (default absent): custom runners to grant
       "catch_vm_error": Bool      // optional (default false): take a VM error as the result
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-spawn-nondet`

Semantics
~~~~~~~~~

Creates a non-deterministic :term:`sub-VM`. Derivation of its meta-properties,
including the *param* ``runner`` and *param* ``custom_runners`` semantics, is
specified in :ref:`gvm-meta-property-derivation`.

A :term:`sub-VM` that ends in a :ref:`gvm-def-vm-error` normally ends its caller
too. With *param* ``catch_vm_error`` set, the caller reads that error as the
call's result instead. A fatal :ref:`gvm-def-vm-error` is never caught: the flag
does not apply to it, and the caller ends regardless.

.. _gvm-def-gl-call-sandbox:

``Sandbox`` Message
-------------------

Executes code in a sandboxed environment. See
:ref:`gvm-meta-property-derivation`.

Payload
~~~~~~~

.. code-block::

   {
     "Sandbox": {
       "data": Bytes,                   // Code/data for sandbox execution
       "runner": String,                // runner to execute; becomes the child's "contract"
       "allow_write_storage": Bool,     // Whether to allow storage writes
       "allow_send_messages": Bool,     // Whether to allow sending messages
       "custom_runners": [String],      // optional (default absent): custom runners to grant
       "changes_on_error": String        // fate of the child's changes on a non-return
     }
   }

Semantics
~~~~~~~~~

Creates a :term:`sub-VM` at the caller's determinism level. Derivation of its
meta-properties, including the *param* ``runner`` and *param*
``custom_runners`` semantics, is specified in
:ref:`gvm-meta-property-derivation`.

The caller receives the sandbox result (:ref:`gvm-def-subvm-result-encoding`)
and may handle both :ref:`gvm-def-vm-error` and :ref:`gvm-def-user-error`.
If the sandbox terminates with :ref:`gvm-def-fatal-vm-error`, the caller
terminates with the same fatal VM error instead.

*param* ``changes_on_error`` says what becomes of the storage writes and
emissions of a sandbox that does not :ref:`gvm-def-return`. ``"inherit"`` is
its only accepted value: the caller keeps them, exactly as it keeps the ones it
made itself. Any other value is a malformed message

.. _gvm-def-gl-call-register-runner:

``RegisterRunner`` Message
--------------------------

Registers a runner archive at runtime, making it available under the
``custom:<hash>`` runner id. The ``<hash>`` is the SHA3-256 of the supplied
``code`` encoded with :doc:`../../04-contract-interface/06-gvm32`.
See :ref:`gvm-def-custom-runner-visibility`.

Payload
~~~~~~~

.. code-block::

   {
     "RegisterRunner": {
       "code": Bytes              // runner archive (zip, raw wasm or commented text)
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`

Semantics
~~~~~~~~~

``RegisterRunner`` performs a load action for ``custom:<hash>`` in the calling
:term:`sub-VM`. If the runner is already loaded, registration is a free no-op
that returns the same runner id. Otherwise
:ref:`gvm-def-consts-value-memory-limiter-consts-runner-load-cost` plus ``code`` length is charged against
the caller's RAM budget before the archive is parsed; on success, the runner
enters the caller's loaded set.

The outcomes, in check order, are:

#. Missing :ref:`gvm-def-det-mode`: the call fails with ``Forbidden``. Nothing
   is charged and no state changes.
#. Insufficient memory for the charge: the :term:`sub-VM` exits with an
   out-of-memory :ref:`gvm-def-vm-error`. Nothing is charged and the runner is
   not registered.
#. Malformed archive: the call fails with a deterministic invalid-contract
   :ref:`gvm-def-vm-error`. The charge is retained until the :term:`sub-VM`
   finishes, and the runner is not in the loaded set.
#. Success: the runner id is returned and the runner is in the caller's loaded
   set.

.. _gvm-def-gl-call-map-file:

``MapFile`` Message
-------------------

Maps a file from a runner into the VM filesystem at runtime, behaving the same as
the ``MapFile`` runner action (see the Python SDK runners documentation). If
``path_in_runner`` ends with ``/`` the whole directory subtree is mapped.

Payload
~~~~~~~

.. code-block::

   {
     "MapFile": {
       "runner": String,          // runner id (name:hash, contract, chain:..., custom:...)
       "path_in_runner": String,  // path within the runner archive
       "path_in_vfs": String      // absolute destination path in the VM filesystem
     }
   }

Mapping into ``/vm/`` is forbidden. See
:ref:`gvm-def-custom-runner-visibility` for ``custom:`` runner resolution.

Resolving *param* ``runner`` performs a load action for that runner. The load is
charged on first load in this :term:`sub-VM` and is free if the runner is
already loaded.

``WebRender`` Message
---------------------

Renders web content using GenVM web module.

Payload
~~~~~~~

.. code-block::

   {
     "WebRender": {
       "mode": String,            // "text", "html", or "screenshot"
       "url": String,             // URL to render
       "post_load_wait": String // Wait duration, e.g. "5s" or "500ms"
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-def-non-det-mode` execution
#. Web module availability

``WebRequest`` Message
----------------------

Makes HTTP requests using GenVM web module.

Payload
~~~~~~~

.. code-block::

   {
     "WebRequest": {
       "method": String,          // "GET", "POST", "HEAD", "DELETE", "OPTIONS", or "PATCH"
       "url": String,             // Request URL
       "headers": Map,            // String -> Bytes mapping of headers
       "body": Bytes | null,      // Optional request body
       "sign": Bool               // Whether to sign the request (default: false)
     }
   }

Response
~~~~~~~~

.. code-block::

   {
     "status": Number,            // HTTP status code
     "headers": Map,              // String -> Bytes mapping of response headers
     "body": Bytes                // Response body
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-def-non-det-mode` execution
#. Web module availability

``ExecPrompt`` Message
----------------------

Executes LLM prompts using GenVM LLM module.

Payload
~~~~~~~

.. code-block::

   {
     "ExecPrompt": {
       "response_format": String, // "text" (default) or "json"
       "prompt": String,          // The prompt text
       "images": Array            // Array of image bytes (max 2)
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-def-non-det-mode` execution
#. LLM module availability

Supports up to 2 images per prompt. Consumes fuel based on LLM usage.

``ExecPromptTemplate`` Message
------------------------------

Executes structured LLM prompt templates with type-specific validation.

Payload
~~~~~~~

One of the following template types:

.. code-block::

   // Comparative template (expects boolean response)
   {
     "ExecPromptTemplate": {
       "template": "EqComparative",
       "leader_answer": String,
       "validator_answer": String,
       "principle": String
     }
   }

   // Non-comparative validator template
   {
     "ExecPromptTemplate": {
       "template": "EqNonComparativeValidator",
       "task": String,
       "criteria": String,
       "input": String,
       "output": String
     }
   }

   // Non-comparative leader template
   {
     "ExecPromptTemplate": {
       "template": "EqNonComparativeLeader",
       "task": String,
       "criteria": String,
       "input": String
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-def-non-det-mode` execution
#. LLM module availability

Comparative templates expect boolean responses. Non-comparative templates expect text responses.

``EmitEvent`` Message
---------------------

Emits blockchain events with topics and data.

Payload
~~~~~~~

.. code-block::

   {
     "EmitEvent": {
       "topics": Array,           // Array of 32-byte topics (max 4)
       "blob": Map                // String -> Calldata mapping of event data
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. GenVM version 0.1.5 or higher

Topics must be exactly 32 bytes each.

``UserError`` Message
---------------------

Triggers contract UserError with custom error message.

Payload
~~~~~~~

.. code-block::

   {
     "UserError": Any           // Error message
   }

Causes VM to exit with ``UserError``. Terminates contract execution immediately.

``Return`` Message
------------------

Returns value from contract execution and terminates.

Payload
~~~~~~~

.. code-block::

   {
     "Return": Calldata           // Return value in calldata format
   }

Causes VM to exit with ``ContractReturn``. Encodes return value using
:ref:`gvm-def-calldata-encoding` format.

.. _gvm-def-gl-call-observable-discretion:

Implementation Discretion
-------------------------

Where a message below says an implementation may ignore it, that discretion
covers only the side effect the message asks for — writing a log line,
recording a timing. The **value returned to the guest** is never at an
implementation's discretion: it is fixed by this specification for the mode the
call is made in, and such a call MUST NOT fail. A message whose result differs
between two conformant implementations would make
:ref:`gvm-def-det-mode` execution diverge across validators; widening or
narrowing what a message returns therefore requires a new
:ref:`GenVM version <gvm-def-contract-version>`.

``Trace.Message`` Message
-------------------------

Logs a debug message with timing information including:

- Custom message text
- Total elapsed time since VM start
- Time elapsed since last trace call

Payload
~~~~~~~

.. code-block::

   {
     "Trace": {
       "Message": String          // Debug message text
     }
   }

.. note::

   Whether anything is logged is implementation-defined (see
   :ref:`gvm-def-gl-call-observable-discretion`); the call returns no value and
   always succeeds.

Requirements
~~~~~~~~~~~~

#. GenVM version 0.1.10 or higher

.. _tracing-runtime-microsec:

``Trace.RuntimeMicroseconds`` Sub-Message
-----------------------------------------

In :ref:`gvm-def-non-det-mode` returns the elapsed execution time in microseconds since VM start.
In :ref:`gvm-def-det-mode`, it returns ``0`` — exposing real elapsed time there
would break determinism. An implementation MAY support a debug mode that
returns real elapsed time instead; such a mode is for local debugging only and
MUST NOT be used on a consensus network.

Payload
~~~~~~~

.. code-block::

   {
     "Trace": "RuntimeMicroseconds"
   }

.. note::

   The returned value is not at an implementation's discretion — in
   :ref:`gvm-def-det-mode` it is exactly ``0`` unless an implementation's debug
   mode permits real elapsed time, and the call never fails (see
   :ref:`gvm-def-gl-call-observable-discretion`).

Requirements
~~~~~~~~~~~~

#. GenVM version 0.1.10 or higher

``Yield`` Message
-----------------

Cooperative yield. Currently a no-op and returns no value; it is reserved for future use in waiting loops.

Payload
~~~~~~~

.. code-block::

   {
     "Yield": null
   }

.. note::

   Whether the implementation actually yields anything is implementation-defined
   (see :ref:`gvm-def-gl-call-observable-discretion`); the call returns no value
   and always succeeds.

Requirements
~~~~~~~~~~~~

#. GenVM version 0.3.0 or higher

.. _get-timestamp:

``GetTimestamp`` Message
------------------------

Returns the current timestamp as the number of seconds since the Unix epoch.

In :ref:`gvm-def-det-mode` it returns the transaction timestamp, keeping the value deterministic across validators.
In :ref:`gvm-def-non-det-mode` it returns the real wall-clock time.

Payload
~~~~~~~

.. code-block::

   {
     "GetTimestamp": null
   }

Returns the timestamp encoded as a :ref:`gvm-def-calldata-encoding` number.

Requirements
~~~~~~~~~~~~

#. GenVM version 0.3.0 or higher
