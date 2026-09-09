.. _contract-execution-flow:

Contract Execution Flow
=======================

This page describes the contract-facing execution model. VM startup and runner
loading are specified in :doc:`../03-vm/01-startup`.

Deployment and Calls
--------------------

#. On deployment, contract code is stored under the contract
   :ref:`genvm-def-root-slot` layout, either in the root code field or through
   its configured code slot. The deployment call is executed with ``is_init``
   set.
#. On a normal call, ``entry_data`` contains
   :ref:`Calldata Encoded <gvm-def-calldata-encoding>` method-call data as
   described by :ref:`gvm-def-contract-call-conv`.
#. The contract runtime dispatches the call and returns a value, a user error,
   or a VM error as specified by :doc:`../03-vm/05-result`.

Entry Kinds
-----------

``entry_kind`` selects the entry mode:

- :ref:`gvm-def-enum-value-entry-kind-main`: normal contract initialization or
  method dispatch.
- :ref:`gvm-def-enum-value-entry-kind-sandbox`: sandbox entry created by a VM
  API call.
- :ref:`gvm-def-enum-value-entry-kind-consensus-stage`: validator-side
  consensus-stage entry created by a non-deterministic VM API call.

The complete startup message is specified in :ref:`gvm-vm-startup-message`.

``CallContract`` Semantics
--------------------------

``CallContract`` performs a read-only call into another contract. It preserves
``sender_address`` and ``origin_address``: the callee observes the same
``sender_address`` as the caller did, not the immediate caller.

The immediate caller is appended to ``stack``. For a top-level entrypoint,
``stack`` is empty; for a nested call, the immediate caller is the last element.
A callee that needs to authorize its immediate caller must inspect ``stack[-1]``
rather than ``sender_address``.

State Visibility
~~~~~~~~~~~~~~~~

A ``CallContract`` child reads committed on-chain storage. It does not observe
the calling transaction's uncommitted writes, including through a self-call.
Direct reads in the same VM observe the current in-transaction state.

Meta-property changes for ``CallContract`` are specified in :ref:`gvm-meta-property-derivation`.
