Storage System
==============

GenVM's storage system provides persistent state management for
intelligent contracts. It is language-agnostic.

Storage Architecture
--------------------

#. Storage is scoped to an address
#. Storage is organized into :term:`Storage Slot`\s: blocks of ``4294967296`` octets (4GB)
#. For given address, each :term:`Storage Slot` has a unique identifier called :term:`SlotID` which is a 32-octet value
#. Reading uninitialized memory returns zeroes
#. Storage is linear, meaning that each slot provides a contiguous block of memory

Default Derivation Algorithm
----------------------------

.. note::

   This is a proposed default algorithm, but using it is not mandatory.

Consider following structure:

.. code-block:: python

   x: str
   y: str

Both ``x`` and ``y`` may occupy arbitrary amount of space. For that reason variable-length content is stored at an *indirection*:
separate :term:`Storage Slot` which :term:`SlotID` is computed based on previous location,
using following algorithm: *sha3_256(slot_id, offset_in_slot_as_4_bytes_little_endian)*.

This means that that it is: *sha3_256(slot_id, [0, 0, 0, 0])* for ``x`` and *sha3_256(slot_id, [0, 0, 0, 4])* for ``y``.
4 is because maximum length of string is bound by 4GB and there is no point in storing it at indirection.
Note that any data that uses an indirection must occupy at least one byte in it's residing slot

.. _genvm-def-root-slot:

Root Slot
---------

:term:`Storage Slot` with :term:`SlotID` of all zeroes is called :ref:`genvm-def-root-slot`. It uses `Default Derivation Algorithm`_ to store
the following data:


- ``major``: (offset :ref:`gvm-def-consts-value-root-offsets-major`) Single octet (``u8``)
  identifying the major version of the public ABI
    that the contract was built against. It is written at deploy time (the value is detected
    from the contract package — see :doc:`04-upgradability` and the impl-spec for the detection flow)
    and read on every load so :term:`GenVM` can refuse to execute a contract whose public ABI
    major does not match the host's ``CURRENT_MAJOR``.
- ``contract_instance``: (offset 1) Reference to the contract instance data.
- ``code``: (offset 2) The contract's code. Slot contains 4 bytes little-endian length followed by data
- ``locked_slots``: (offset 3) A list of storage :term:`SlotID`\s that cannot be modified by non-upgraders.
    Slot contains 4 bytes little-endian length followed length arrays of 32 byte :term:`SlotID`\s
- ``upgraders``: (offset 4) A list of addresses that are authorized to modify the contract code and locked slots.
    Slot contains 4 bytes little-endian length followed length arrays of 20 byte addresses
- ``code_slot``: (offset 5) A raw 32-byte :term:`SlotID`. If it is all-zero (the default),
    the contract code is read from the ``code`` slot (offset 2); otherwise the code is read from
    the slot it points to (same 4-byte-length-prefixed layout). This lets a contract serve its
    code from an arbitrary slot, including one shared via a ``chain:<address>:<a|f>:<slot>`` runner id.
- ``permissions``: (offset 37) A 32-byte (``u256``) little-endian permission bitfield read by
    the executor at the start of every load. Bit ``n`` corresponds to the permission whose value
    is ``n`` (currently only bit ``0``, ``can_use_balance_for_message_fees``). It is not reserved:
    contracts may set it (see ``Root.get_permission`` / ``Root.set_permission`` in the Python SDK).

Offsets are byte offsets into the root slot, so ``code_slot`` (32 bytes) occupies offsets ``5``
through ``36`` and ``permissions`` (32 bytes) occupies offsets ``37`` through ``68``; the next
field would start at offset ``69``.

Offsets at and above ``69`` are reserved for future :term:`GenVM` use and should remain zero:
a contract must not store data in them, otherwise a future :term:`GenVM` version that starts
reading those offsets may break the contract. Contract runtimes that need scratch space for
bootstrapping should derive a separate sub-slot instead (see ``Root.get_vacant_slot`` in the
Python SDK).

Upgrade permissions and slot locking are described in :doc:`04-upgradability`.
