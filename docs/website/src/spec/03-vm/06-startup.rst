.. _gvm-vm-startup:

Startup
=======

This page specifies VM startup behavior that is not part of the contract method
interface. Runner syntax is specified in :doc:`../02-execution-environment/04-runners`.

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

The executable WASM receives one
:ref:`Calldata Encoded <gvm-def-calldata-encoding>` message on standard input.
The message contains:

- ``contract_address``, ``sender_address``, ``origin_address``
- ``stack``: the view-call stack, empty for a top-level entry
- ``chain_id``, ``value``, ``is_init``, ``datetime``
- ``entry_kind``: one of the :ref:`entry-kind ABI values <gvm-def-enum-value-entry-kind-main>`
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

Sub-VM Startup
--------------

``CallContract``, ``RunNondet``, and sandbox execution create child
:term:`sub-VM` instances with derived startup messages, storage views, runner
sources, and permissions. Their permission changes are specified in
:doc:`05-permissions`.

Sub-VM Startup Action
~~~~~~~~~~~~~~~~~~~~~

Sub-VM startup occurs when a :term:`sub-VM` is created. It occurs for:

- the initial entry
- ``CallContract``
- ``Sandbox``
- ``RunNondet``

The action enforces the :ref:`gvm-def-consts-value-top-limits-vm-recursion`
limit. If the limit is exceeded, startup fails with
:ref:`gvm-def-str-trie-value-vm-error-out-of-vm-recursion`.

For ``RunNondet``, the action also consumes one
:ref:`gvm-def-consts-value-top-limits-nondet-blocks` slot. If no slot remains,
the call fails with :ref:`gvm-def-str-trie-value-vm-error-out-of-nondet-blocks`.

After these checks, startup applies any custom-runner grants for the new
:term:`sub-VM`, then performs the runner load action for the selected entry
runner and continues with `Runner Startup`_.
