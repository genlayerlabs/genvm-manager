Host Loop Pseudocode
====================

**Constants**:

::

   ACCOUNT_ADDR_SIZE = 20
   SLOT_ID_SIZE = 32

**Data Encoding Functions**:

::

   write_byte_slice(arr):
     write_u32_le len(arr)
     write_bytes arr

   read_slice():
     len := read_u32_le
     data := read_bytes(len)
     return data

Multi-Host
~~~~~~~~~~

GenVM supports multiple host connections. Each host method is routed to a
specific host connection based on a ``method_hosts`` mapping in
``ExecutionData``. The mapping is a byte array where each index corresponds to a
method ID and the value is the host connection index. When the index is out of
bounds or the array is empty, host 0 is used as the default.

The executor accepts multiple ``--host`` arguments. Each host connection
independently runs the protocol described below.

In the manager deployment, host 0 is the node's host loop and host 1 is a
socketpair to the manager. The manager serves ``consume_result``, ``run_nested``
and -- by default -- ``resolve_call_contract_executor`` on host 1.

``resolve_call_contract_executor`` moves to host 0 only when the run request sets
``hook_cross_contract_calls`` (see :doc:`manager-socket`). Otherwise the manager
answers it with a null reply, which keeps every ``CallContract`` in-process. A
host that does not route calls across major boundaries therefore need not
implement that method at all.

A non-null answer is a calldata map naming the line the callee runs on, tagged on
``kind``: ``{"kind": "major", "major": u32}`` resolves by the manifest's rules,
falling back to the newest line when none provides that major, while
``{"kind": "version", "version": str}`` names a line outright -- an executor
directory as it stands, or a ``re:``-prefixed regex over manifest version keys,
which fails the run if it matches nothing. Only a version can mean one
particular line, since every line released so far is semver major ``0``.
Executor lines carry the payload without reading it; only the manager decodes
it.

Connection Hello Data
~~~~~~~~~~~~~~~~~~~~~

``ExecutionData`` carries ``host_hello_data``, an array of byte strings
indexed by host connection index (the same index space ``method_hosts``
values point into). Immediately after connecting host *i*, before writing the
first method byte, the executor writes entry *i* to that connection
**verbatim** -- raw bytes, no length prefix or framing. A missing, short, or
empty entry means nothing is written; an empty (or absent) array is
byte-identical to a connection without hello data. The bytes are supplied by
the host in the run request (see :doc:`manager-socket`), so the host knows
their length; a host wanting structure embeds its own framing.

Protocol Loop
~~~~~~~~~~~~~

The :term:`host` processes requests in a loop. Each host connection runs
independently, handling only the methods routed to it. The loop ends when the
executor closes the connection (EOF on the method-id read): on a stream
socket all written data precedes the FIN, so every response the host owes has
been requested and every result byte has arrived by then. The executor
flushes all host connections before exiting. Completion status (exit code,
result, clean-finish vs crash) is reported by the manager's terminal event
(:doc:`manager-socket`), not by a host method:

::

   loop:
     method_id := read_byte
     match method_id
       json/methods/storage_read:
         read_type := read_byte as json/storage_view
         address := read_bytes(ACCOUNT_ADDR_SIZE)
         slot := read_bytes(SLOT_ID_SIZE)
         offset := read_u32_le
         len := read_u32_le
         data, err := host_storage_read(read_type, address, slot, offset, len)
         if err != json/errors/ok:
           write_byte err
         else:
           write_byte json/errors/ok
           write_bytes data # must be exactly len in size

       json/methods/consume_result:
         host_result := read_slice()
         # this is needed to ensure that genvm doesn't close socket before all data is read
         write_byte 0x00

       json/methods/consume_time_fee_gen_wei:
         time_fee_gen_wei := read_u256_le
         host_consume_time_fee_gen_wei(time_fee_gen_wei)
         # note: this method doesn't send any response

       json/methods/external_call:
         address := read_bytes(ACCOUNT_ADDR_SIZE)
         calldata := read_slice()
         result, err := host_external_call(address, calldata)
         if err != json/errors/ok:
           write_byte err
         else:
           write_byte json/errors/ok
           write_byte_slice result

       json/methods/get_balance_gen_wei:
         address := read_bytes(ACCOUNT_ADDR_SIZE)
         balance, err := host_get_balance_gen_wei(address)
         if err != json/errors/ok:
           write_byte err
         else:
           write_byte json/errors/ok
           write_bytes balance.to_le_bytes(32) # 256-bit integer

       json/methods/resolve_call_contract_executor:
         address := read_bytes(ACCOUNT_ADDR_SIZE)
         state := read_byte as json/storage_view
         advisory_major := read_byte
         payload, err := host_resolve_call_contract_executor(
           address, state, advisory_major)
         if err != json/errors/ok:
           write_byte err
         else:
           write_byte json/errors/ok
           # Calldata optional bytes preserve null vs an empty payload.
           write_byte_slice calldata_encode(payload)

       json/methods/run_nested:
         envelope := calldata_decode(read_slice())
         # The envelope carries no version of its own -- every executor line
         # compiles the one definition. The manager validates the routing
         # payload, resolves the executor at the inherited timestamp, and mints
         # a fresh execution id. It uses the outer deadline/cancellation and no
         # permit.
         child := manager_start_nested_sync(
           envelope,
           storage_host = host_0,
           result_host = host_1)
         reply := {
           result: child.result,
           # The child's sub-VM small hash, not its execution hash: the caller
           # folds this, and it must not depend on the route the host chose.
           small_hash: child.small_hash,
           effect_free: child.has_no_effects
         }
         write_byte_slice calldata_encode(reply)

       json/methods/get_remaining_time_fee_gen_wei:
         time_fee_gen_wei, err := host_get_remaining_time_fee_gen_wei()
         if err != json/errors/ok:
           write_byte err
         else:
           write_byte json/errors/ok
           write_bytes time_fee_gen_wei.to_le_bytes(32) # 256-bit unsigned, little-endian, always 32 bytes

       json/methods/notify_nondet_disagreement:
         call_no := read_u32_le
         host_notify_nondet_disagreement(call_no)
         # note: this method doesn't send any response
