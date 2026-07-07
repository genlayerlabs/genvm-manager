.. _gvm-vm-startup:

Startup
=======

This page specifies how :term:`sub-VM`\s start: the top-level entry, the
startup message, runner initialization, and the derivation of meta-properties
when a child :term:`sub-VM` is created. The meta-property fields themselves are
defined in :doc:`02-meta-properties`. Runner syntax and load mechanics are
specified in :doc:`../02-execution-environment/04-runners`.

Top-Level Startup
-----------------

Before executing the root :term:`sub-VM`, :term:`GenVM`:

#. Reads the contract's locked slots and root-slot data.
#. Resolves the contract code from deployment input or from the contract's
   configured code slot.
#. Checks upgrade and ABI-major compatibility as specified in
   :doc:`../04-contract-interface/04-upgradability`.
#. Reads contract-owned permission bits from the root slot and combines them
   with node-granted VM permissions.
#. Creates a deterministic root VM with the initial message described below.

If code is supplied with the execution request, that code is the runner source
for the deployment execution. Otherwise, the runner source is the contract code
resolved from storage.

.. _gvm-vm-startup-message:

Startup Message
---------------

The executable WASM receives one :ref:`gvm-def-calldata-encoding` message on
standard input.
The message contains:

- ``contract_address``, ``sender_address``, ``origin_address``
- ``signer_address``: the externally-owned account that signed the
  transaction. :term:`GenVM` treats it as opaque and forwards it unchanged to
  every child :term:`sub-VM`.
- :ref:`gvm_vm_field_stack`: the view-call stack, empty for a top-level entry
- ``chain_id``, ``value``, ``is_init``, ``datetime``
- ``entry_kind``: one of :ref:`gvm-def-enum-entry-kind`
- ``entry_data``: entry payload bytes
- ``entry_stage_data``: consensus-stage payload data

For top-level execution, ``entry_kind`` is
:ref:`gvm-def-enum-value-entry-kind-main` and ``entry_stage_data`` is absent
data. Sub-VM operations may create sandbox or consensus-stage messages.

Runner Startup
--------------

The selected runner is loaded and its initialization actions are applied until
:ref:`gvm-def-start-wasm` is reached. Actions may map files, set process
arguments, add environment variables, or link additional WASM modules. Linked
modules that export ``_initialize`` execute that function before startup
continues.

The ``StartWasm`` action instantiates the executable WASM module. Its ``_start``
entrypoint then consumes the startup message and produces the VM result.

.. _gvm-vm-subvm-creation:
.. _gvm-meta-property-derivation:

Sub-VM Creation
---------------

A new :term:`sub-VM` is created for:

- the initial entry
- :ref:`gvm-def-gl-call-call-contract`
- :ref:`gvm-def-gl-call-sandbox`
- :ref:`gvm-def-gl-call-run-nondet`

Creation is rejected with
:ref:`gvm-def-str-trie-value-vm-error-out-of-vm-recursion` if the new
:ref:`gvm_vm_field_depth` is greater than or equal to
:ref:`gvm-def-consts-value-top-limits-vm-recursion`.

Each :ref:`gvm-def-gl-call-run-nondet` additionally allocates the next
execution-wide ``call_no``, starting at zero. If ``call_no`` is greater than or
equal to :ref:`gvm-def-consts-value-top-limits-nondet-blocks`, the call fails
with :ref:`gvm-def-str-trie-value-vm-error-out-of-nondet-blocks`.

After these checks, startup applies the `Custom-Runner Grants`_ for the new
:term:`sub-VM`, then performs the runner load action for the entry runner and
continues with `Runner Startup`_.

Meta-Property Derivation
------------------------

Every meta-property of a child :term:`sub-VM` starts as an independent copy of
the parent's value; later changes in the child never affect the parent. The
subsections below list only the fields that deviate from that copy. Two
deviations apply to every child:

- :ref:`gvm_vm_field_depth` is ``parent.depth + 1``
- :ref:`gvm_vm_field_det_subvm_hashes` starts empty

The initial entry has no parent, so all of its fields are given explicitly.

Initial Entry
~~~~~~~~~~~~~

- :ref:`gvm_vm_field_stack` is empty; :ref:`gvm_vm_field_depth` is ``0``.
- :ref:`gvm_vm_field_permissions` are the node-granted permission
  meta-properties, plus :ref:`gvm-perm-use-balance-for-message-fees` read from
  the contract's root slot.
- :ref:`gvm_vm_field_state_mode` is the default storage view.
- :ref:`gvm_vm_field_topmost_runner_id` is the deployment runner or the
  contract's accepted code runner.
- :ref:`gvm_vm_field_det_subvm_hashes` and
  :ref:`gvm_vm_field_granted_custom` are empty.

:ref:`gvm-def-gl-call-call-contract`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The child is read-only.

- :ref:`gvm_vm_field_permissions` deviations:

  - :ref:`gvm-perm-write-storage` is false
  - :ref:`gvm-perm-send-messages` is false
  - :ref:`gvm-perm-spawn-nondet` is false
  - :ref:`gvm-perm-use-balance-for-message-fees` is false

- :ref:`gvm_vm_field_state_mode` is the requested storage view (*param*
  ``state``); a request of :ref:`gvm-def-enum-value-storage-type-default`
  keeps the parent's value (the plain copy rule), so by default the callee
  observes a view at least as recent as its caller's. Because the child
  cannot write, its :ref:`gvm-def-enum-value-storage-type-default` view is
  the accepted state: it never includes the calling transaction's uncommitted
  writes (see :ref:`contract-execution-flow`).
- :ref:`gvm_vm_field_topmost_runner_id` is the callee's contract runner.
- :ref:`gvm_vm_field_granted_custom` is the caller's entire loaded
  custom-runner set (see `Custom-Runner Grants`_).
- The child's startup message is a copy of the caller's, except:

  - ``contract_address`` is the callee's address
  - :ref:`gvm_vm_field_stack` additionally has the caller's
    ``contract_address`` appended
  - ``entry_data`` is the :ref:`Calldata Encoded <gvm-def-calldata-encoding>`
    method call
  - ``value`` is ``0``
  - ``is_init`` is false

:ref:`gvm-def-gl-call-sandbox`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The child runs at the same determinism level as its parent and cannot exceed
the parent's privileges: each ``allow_*`` payload flag can only retain an
inherited permission, never add one.

- :ref:`gvm_vm_field_permissions` deviations:

  - :ref:`gvm-perm-write-storage` is cleared unless *param*
    ``allow_write_storage`` is set
  - :ref:`gvm-perm-spawn-nondet` is false
  - :ref:`gvm-perm-call-others` is false
  - :ref:`gvm-perm-send-messages` is cleared unless *param*
    ``allow_send_messages`` is set
  - :ref:`gvm-perm-use-balance-for-message-fees` is cleared unless *param*
    ``allow_send_messages`` is set: balance-funded fees only affect message
    emission, so they are gated by the same flag
  - :ref:`gvm-perm-register-runners` is cleared unless *param*
    ``allow_register_runners`` is set

- :ref:`gvm_vm_field_topmost_runner_id` is the *param* ``runner``, resolved in
  the caller's scope.
- :ref:`gvm_vm_field_granted_custom` is derived from *param*
  ``custom_runners`` as specified in `Custom-Runner Grants`_.

:ref:`gvm-def-gl-call-run-nondet`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The child executes in :ref:`gvm-def-non-det-mode`.

- :ref:`gvm_vm_field_permissions` are all false, including
  :ref:`gvm-perm-deterministic`.
- :ref:`gvm_vm_field_state_mode` is the default storage view.
- :ref:`gvm_vm_field_topmost_runner_id` is the *param* ``runner``, resolved in
  the caller's scope; when absent, the caller's own runner is used.
- :ref:`gvm_vm_field_granted_custom` is derived from *param*
  ``custom_runners`` as specified in `Custom-Runner Grants`_.

.. _gvm-meta-property-custom-runners:

Custom-Runner Grants
--------------------

At creation, the child's :ref:`gvm_vm_field_granted_custom` is derived from
the caller's loaded custom-runner set
(see :ref:`gvm-def-custom-runner-visibility`):

- :ref:`gvm-def-gl-call-sandbox` and :ref:`gvm-def-gl-call-run-nondet`
  children receive the runners named by *param* ``custom_runners``:

  - when absent, every ``custom:`` entry of the caller's loaded set is granted;
  - when present, exactly the listed runners are granted. Every listed runner
    must be a ``custom:<hash>`` id loaded in the caller, without duplicates;
    a list containing any other kind of id (including ``name:hash`` and
    ``chain:``) is a :ref:`gvm-def-vm-error`;
  - if the *param* ``runner`` is itself a ``custom:`` id, it must be loaded in
    the caller and is granted implicitly.

- :ref:`gvm-def-gl-call-call-contract` children receive the caller's entire
  custom set.

Each grant is a load action in the child, charged against the child's RAM
budget. Grants are applied before the entry-runner load, so a ``custom:``
entry point that is also granted is charged once.

Grants never flow back: when a child :term:`sub-VM` finishes, the parent's
loaded set is unchanged, even if the child registered runners.
