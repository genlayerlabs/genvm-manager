.. _gvm-meta-properties:
.. _gvm-permissions:

Meta-Properties
===============

Each :term:`sub-VM` carries VM meta-properties. Some are part of the startup
message; others are VM-internal. This page defines those fields; how they are
derived when a :term:`sub-VM` starts is specified in
:ref:`gvm-meta-property-derivation`.

Most permission meta-properties are granted by the node for the initial entry.
The exception is :ref:`gvm-perm-use-balance-for-message-fees`, which is owned by
the contract: it is stored in the contract's root slot and read before
execution begins.

Execution Meta-Properties
-------------------------

.. _gvm_vm_field_stack:

``stack``
~~~~~~~~~

Startup-message field containing the view-call stack of contract addresses.

.. _gvm_vm_field_depth:

``depth``
~~~~~~~~~

Internal field containing the nesting depth of the :term:`sub-VM`.

.. _gvm_vm_field_permissions:

``permissions``
~~~~~~~~~~~~~~~

Internal field containing the permission meta-properties granted to the
:term:`sub-VM`.

.. _gvm_vm_field_state_mode:

``state_mode``
~~~~~~~~~~~~~~

Internal field selecting the storage view used by reads
(a :ref:`gvm-def-enum-storage-type`).

.. _gvm_vm_field_topmost_runner_id:

``topmost_runner_id``
~~~~~~~~~~~~~~~~~~~~~

Internal field selecting the entry runner for this :term:`sub-VM`.

.. _gvm_vm_field_det_subvm_hashes:

``det_subvm_hashes``
~~~~~~~~~~~~~~~~~~~~

Internal accumulator for deterministic child :term:`sub-VM` result hashes; see
:ref:`gvm-def-subvm-hash`.

.. _gvm_vm_field_granted_custom:

``granted_custom``
~~~~~~~~~~~~~~~~~~

Internal field containing custom runners granted to this :term:`sub-VM` at
startup; see :ref:`gvm-meta-property-custom-runners`.

Custom runners are *not* permission meta-properties: they are tracked by the
:term:`sub-VM`'s loaded-runner set
(:ref:`gvm-def-custom-runner-visibility`) and by this field.

Permission Meta-Properties
--------------------------

.. _gvm-perm-deterministic:

``deterministic``
~~~~~~~~~~~~~~~~~

When true, the :term:`sub-VM` executes in :ref:`gvm-def-det-mode`. Storage
writes, message sends, contract calls, event emission, and runner registration
require this meta-property where specified below.

.. _gvm-perm-write-storage:

``write_storage``
~~~~~~~~~~~~~~~~~

Allows writing contract storage slots. Requires
:ref:`gvm-perm-deterministic`.

This meta-property also gates ``EmitEvent``: a :term:`sub-VM` may emit events
iff it can write storage.

.. _gvm-perm-send-messages:

``send_messages``
~~~~~~~~~~~~~~~~~

Allows sending messages to other addresses. Required by ``EthSend``,
``PostMessage``, and ``DeployContract``. Requires
:ref:`gvm-perm-deterministic`.

.. _gvm-perm-call-others:

``call_others``
~~~~~~~~~~~~~~~

Allows calling other contracts. Required by ``EthCall`` and
:ref:`gvm-def-gl-call-call-contract`.
Requires :ref:`gvm-perm-deterministic`.

.. _gvm-perm-spawn-nondet:

``spawn_nondet``
~~~~~~~~~~~~~~~~

Allows spawning :ref:`gvm-def-non-det-mode` :term:`sub-VM` instances via
:ref:`gvm-def-gl-call-run-nondet`.

.. _gvm-perm-register-runners:

``register_runners``
~~~~~~~~~~~~~~~~~~~~

Allows registering runner archives at runtime via
:ref:`gvm-def-gl-call-register-runner`. Requires :ref:`gvm-perm-deterministic`.

.. _gvm-perm-use-balance-for-message-fees:

``can_use_balance_for_message_fees``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Allows the contract to draw on its own balance to pay fees for outgoing
internal messages (``PostMessage``, ``DeployContract``). The flag has no effect
on external ``EthSend`` messages.

Unlike the meta-properties above, this one is stored as a bit in the root slot's
inline ``permissions`` bitfield and read before execution begins. The bit offset
is the corresponding member of :ref:`gvm-def-enum-permissions`.
