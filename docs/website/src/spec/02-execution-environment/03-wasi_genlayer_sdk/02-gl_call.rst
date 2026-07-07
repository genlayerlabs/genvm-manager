``gl_call`` Messages
====================

``EthSend`` Message
-------------------

Sends transaction to Ethereum address with optional value transfer.

Payload
~~~~~~~

.. code-block::

   {
     "EthSend": {
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

``EthCall`` Message
-------------------

Calls Ethereum contract method (read-only operation).

Payload
~~~~~~~

.. code-block::

   {
     "EthCall": {
       "address": Address,      // 20-byte target contract address
       "calldata": Bytes        // EVM calldata
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-call-others`

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
       "state": Number          // Storage type: 0=default, 1=latest_final, 2=latest_non_final
     }
   }

When ``state`` is ``0`` (``default``) :term:`GenVM` resolves it to ``latest_non_final``.
The motivation is that the caller has already observed (and possibly modified)
non-final state in the current transaction, so reading anything older than
``latest_non_final`` for an in-transaction call would expose stale data and
break causality between the caller and callee.

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-call-others`

Creates new :term:`sub-VM` instance for contract execution. See :ref:`gvm-permissions` for permission inheritance details.

.. _gvm-def-post-message:

``PostMessage`` Message
-----------------------

Posts message to GenLayer contract for later execution.

When GenVM forwards this message it derives a :ref:`call_key <gvm-def-call-key>`
from the ``method`` field of ``calldata`` and attaches it to the emitted
message. The :ref:`call_key <gvm-def-call-key>` is the function-selector
analog used to identify the target method.

Payload
~~~~~~~

.. code-block::

   {
     "PostMessage": {
       "address": Address,      // 20-byte target contract address
       "calldata": Calldata,    // Method call in calldata format
       "value": U256,           // Wei to transfer
       "on": String,            // "finalized" or "accepted"
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
     "leader_timeunits_allocation": U256,     // per-round leader time units
     "validator_timeunits_allocation": U256,  // per-round validator time units
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
  otherwise the call fails with ``Inbalance``.
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
  2\ :sup:`96`; counts (``leader_timeunits_allocation``,
  ``validator_timeunits_allocation``, each ``rotations`` entry) below
  2\ :sup:`32`. These bounds keep the metered floor within ``U256``.

Metering additionally enforces node-configured floors, surfaced as ``VMError``\ s:

- ``fee below_minimum`` — a per-phase time-unit allocation below
  ``node.minTimeUnitsPerPhase``, or a non-zero ``execution_budget_per_round`` below
  ``node.messageBudgetFloor`` (the chain's ``BudgetTooLow``).
- ``fee too_many_rounds`` — ``rotations`` implies more consensus rounds than the
  node's validator table supports (on-chain ``MAX_ROUNDS``).

``DeployContract`` Message
--------------------------

Deploys new intelligent contract to blockchain.

Payload
~~~~~~~

.. code-block::

   {
     "DeployContract": {
       "calldata": Calldata,    // Constructor arguments in calldata format
       "code": Bytes,           // Contract bytecode
       "value": U256,           // Wei to transfer
       "on": String,            // "finalized" or "accepted"
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

Supports CREATE2-style deployment with salt nonce for deterministic addressing.
``use_balance`` / ``fee_params`` behave as for :ref:`PostMessage <gvm-gl-call-balance-fees>`.

``RunNondet`` Message
---------------------

Executes non-deterministic code with leader/validator consensus.
Creates :ref:`gvm-def-non-det-mode` VM instance with restricted permissions.
See :doc:`../../03-vm/04-determinism-mode-switching` and :ref:`gvm-permissions` for more details.

Payload
~~~~~~~

.. code-block::

   {
     "RunNondet": {
       "data_leader": Bytes,       // Code/data for leader execution
       "data_validator": Bytes,    // Code/data for validator execution
       "runner": String,           // optional (default "contract"): runner to execute
       "custom_runners": [String]  // optional (default absent): custom runners to grant
     }
   }

``runner`` selects what the non-deterministic :term:`sub-VM` executes; it is
resolved in the calling scope and becomes the child's ``contract`` id. When
absent, the caller's own runner is executed (previous behavior).

``custom_runners`` grants custom runners from the parent's loaded custom
runners to the child (see :ref:`gvm-def-custom-runner-visibility`): when absent,
the child inherits every ``custom:`` runner in the caller's loaded set; when
present, exactly the listed runners. Every element must be a ``custom:<hash>``
id, without duplicates, and present in the caller's loaded set — otherwise the
call fails with a :ref:`gvm-def-vm-error`. If ``runner`` is a ``custom:`` id it
must be loaded in the caller and is granted implicitly. Each grant is a load
action in the child, charged to the child's RAM budget; because inherited loads
run before the main-runner load, a ``custom:`` entry point is not charged
twice.

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-spawn-nondet`

``Sandbox`` Message
-------------------

Executes code in sandboxed environment with restricted permissions.

Payload
~~~~~~~

.. code-block::

   {
     "Sandbox": {
       "data": Bytes,                   // Code/data for sandbox execution
       "runner": String,                // runner to execute; becomes the child's "contract"
       "allow_write_storage": Bool,     // Whether to allow storage writes
       "allow_send_messages": Bool,     // Whether to allow sending messages
       "allow_register_runners": Bool,  // Whether to allow registering runners
       "custom_runners": [String]       // optional (default absent): custom runners to grant
     }
   }

Creates isolated VM instance. See :ref:`gvm-permissions` for permission inheritance details.

``custom_runners`` behaves exactly as for ``RunNondet``: absent grants every
``custom:`` runner in the caller's loaded set, a list grants exactly that
(validated) subset, and a ``custom:`` ``runner`` is granted implicitly
(see :ref:`gvm-def-custom-runner-visibility`). Each grant is a load action in
the sandbox, charged to its RAM budget (a ``custom:`` entry point is not
charged twice). Runners registered inside the sandbox do **not** flow back into
the caller's loaded set after it returns.

``RegisterRunner`` Message
--------------------------

Registers a runner archive at runtime, making it available under the
``custom:<hash>`` runner id. The ``<hash>`` is the SHA3-256 of the supplied
``code`` encoded with :doc:`../../04-contract-interface/06-gvm32`.

Registration performs a load action for ``custom:<hash>`` in the calling
:term:`sub-VM`. The hash is computed first. If ``custom:<hash>`` is already in
the caller's loaded set, registration is a free no-op that returns the same
runner id. Otherwise :ref:`gvm-def-const-runner-load-cost` plus ``code`` length is charged
against the caller's RAM budget **before** the archive is parsed; on success
the runner enters the caller's loaded set (see
:ref:`gvm-def-custom-runner-visibility`). Returns the resulting runner id
(calldata-encoded string).

Payload
~~~~~~~

.. code-block::

   {
     "RegisterRunner": {
       "code": Bytes              // runner archive (ustar/zip or commented text)
     }
   }

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-deterministic`
#. :ref:`gvm-perm-register-runners`

Error guarantees
~~~~~~~~~~~~~~~~

The outcomes, in the order they are checked:

#. **Missing** :ref:`gvm-def-det-mode` **or** :ref:`gvm-perm-register-runners`:
   the call fails with a ``Forbidden`` error. Nothing is charged and no state
   changes.
#. **Insufficient memory** for the charge: the :term:`sub-VM` exits with an
   out-of-memory :ref:`gvm-def-vm-error`. Nothing is charged and the runner is
   not registered.
#. **Malformed archive** (parse failure): the call fails with a deterministic
   invalid-contract :ref:`gvm-def-vm-error`. The charge is retained (released
   only when the :term:`sub-VM` finishes, like any charge); the runner is
   **not** in the loaded set and is **not** resolvable. Parse errors depend only
   on the ``code`` bytes, never on schedule or cache state.
#. **Success**: the runner id is returned and the runner is in the caller's
   loaded set. Registering identical ``code`` again in the same :term:`sub-VM`
   is free and returns the same id.

``MapFile`` Message
-------------------

Maps a file from a runner into the VM filesystem at runtime, behaving the same as
the ``MapFile`` runner action (see :doc:`../../../python-sdk` runners). If
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

Mapping into ``/vm/`` is forbidden. Resolving a ``chain:`` runner reads another
contract's storage, so this requires :ref:`gvm-perm-read-storage`.

Resolving the ``runner`` performs a load action for it (see
:ref:`gvm-def-custom-runner-visibility`), charged on its first load in this
:term:`sub-VM` and free if it is already loaded.

Requirements
~~~~~~~~~~~~

#. :ref:`gvm-perm-read-storage`

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
       "wait_after_loaded": String // Wait duration, e.g. "5s" or "500ms"
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

Causes VM to exit with ``ContractReturn``. Encodes return value using :ref:`Calldata Encoded <gvm-def-calldata-encoding>` format.

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

   Implementations may choose to ignore this message and return an error.

Requirements
~~~~~~~~~~~~

#. GenVM version 0.1.10 or higher
#. :term:`GenVM` implementation is allowed ignore this message

.. _tracing-runtime-microsec:

``Trace.RuntimeMicroSec`` Sub-Message
-------------------------------------

In :ref:`gvm-def-non-det-mode` returns the elapsed execution time in microseconds since VM start.
In :ref:`gvm-def-det-mode`, it returns ``0`` — exposing real elapsed time there would break determinism. The sole exception is the ``unsafe-tracing`` debug level (see the executor "Debug modes" section), which returns real elapsed time even in deterministic mode; that level is for local debugging only and must never be used on a consensus network.

Payload
~~~~~~~

.. code-block::

   {
     "Trace": "RuntimeMicroSec"
   }

.. note::

   Implementations may choose to ignore this message and return an error.

Requirements
~~~~~~~~~~~~

#. GenVM version 0.1.10 or higher
#. :term:`GenVM` implementation is allowed ignore this message

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

   Implementations may choose to ignore this message.

Requirements
~~~~~~~~~~~~

#. GenVM version 0.3.0 or higher
#. :term:`GenVM` implementation is allowed ignore this message

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

Returns the timestamp encoded as a :ref:`Calldata Encoded <gvm-def-calldata-encoding>` number.

Requirements
~~~~~~~~~~~~

#. GenVM version 0.3.0 or higher
