Constants
=========

.. _gvm-def-enum-result-code:

result_code
-----------

Type: u8

.. _gvm-def-enum-value-result-code-return:

return
~~~~~~

Value: ``0``

.. _gvm-def-enum-value-result-code-user-error:

user_error
~~~~~~~~~~

Value: ``1``

.. _gvm-def-enum-value-result-code-vm-error:

vm_error
~~~~~~~~

Value: ``2``

.. _gvm-def-enum-value-result-code-internal-error:

internal_error
~~~~~~~~~~~~~~

Value: ``3``

.. _gvm-def-enum-storage-type:

storage_type
------------

Type: u8

.. _gvm-def-enum-value-storage-type-default:

default
~~~~~~~

Value: ``0``

.. _gvm-def-enum-value-storage-type-latest-final:

latest_final
~~~~~~~~~~~~

Value: ``1``

.. _gvm-def-enum-value-storage-type-latest-non-final:

latest_non_final
~~~~~~~~~~~~~~~~

Value: ``2``

.. _gvm-def-enum-entry-kind:

entry_kind
----------

Type: u8

.. _gvm-def-enum-value-entry-kind-main:

main
~~~~

Value: ``0``

.. _gvm-def-enum-value-entry-kind-sandbox:

sandbox
~~~~~~~

Value: ``1``

.. _gvm-def-enum-value-entry-kind-consensus-stage:

consensus_stage
~~~~~~~~~~~~~~~

Value: ``2``

.. _gvm-def-consts-memory-limiter-consts:

memory_limiter_consts
---------------------

Type: u32

.. _gvm-def-consts-value-memory-limiter-consts-table-entry:

table_entry
~~~~~~~~~~~

Value: ``64``

.. _gvm-def-consts-value-memory-limiter-consts-file-mapping:

file_mapping
~~~~~~~~~~~~

Value: ``256``

.. _gvm-def-consts-value-memory-limiter-consts-fd-allocation:

fd_allocation
~~~~~~~~~~~~~

Value: ``96``

.. _gvm-def-consts-root-offsets:

root_offsets
------------

Type: u32

.. _gvm-def-consts-value-root-offsets-major:

major
~~~~~

Value: ``0``

.. _gvm-def-consts-value-root-offsets-contract:

contract
~~~~~~~~

Value: ``1``

.. _gvm-def-consts-value-root-offsets-code:

code
~~~~

Value: ``2``

.. _gvm-def-consts-value-root-offsets-locked-slots:

locked_slots
~~~~~~~~~~~~

Value: ``3``

.. _gvm-def-consts-value-root-offsets-upgraders:

upgraders
~~~~~~~~~

Value: ``4``

.. _gvm-def-consts-value-root-offsets-code-slot:

code_slot
~~~~~~~~~

Value: ``5``

.. _gvm-def-consts-top-limits:

top_limits
----------

Type: u32

.. _gvm-def-consts-value-top-limits-nondet-blocks:

nondet_blocks
~~~~~~~~~~~~~

Value: ``4096``

.. _gvm-def-consts-value-top-limits-locked-slots:

locked_slots
~~~~~~~~~~~~

Value: ``256``

.. _gvm-def-consts-value-top-limits-upgraders:

upgraders
~~~~~~~~~

Value: ``32``

.. _gvm-def-consts-value-top-limits-vm-recursion:

vm_recursion
~~~~~~~~~~~~

Value: ``512``

.. _gvm-def-consts-value-top-limits-web-request-min-space:

web_request_min_space
~~~~~~~~~~~~~~~~~~~~~

Value: ``65536``

.. _gvm-def-consts-value-top-limits-web-render-min-space:

web_render_min_space
~~~~~~~~~~~~~~~~~~~~

Value: ``134217728``

.. _gvm-def-consts-value-top-limits-max-fds:

max_fds
~~~~~~~

Value: ``1024``

.. _gvm-def-enum-special-method:

special_method
--------------

Type: str

.. _gvm-def-enum-value-special-method-get-schema:

get_schema
~~~~~~~~~~

Value: ``#get-schema``

.. _gvm-def-enum-value-special-method-errored-message:

errored_message
~~~~~~~~~~~~~~~

Value: ``#error``

.. _gvm-def-str-trie-vm-error:

vm_error
--------

Type: str_trie

.. _gvm-def-str-trie-value-vm-error-timeout:

``timeout``
~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-absent-leader-nondet-output:

``absent_leader_nondet_output``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-exit-code:

``exit_code``
~~~~~~~~~~~~~

Param: i32

.. _gvm-def-str-trie-value-vm-error-wasm-trap:

``wasm_trap``
~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-unreachable:

``wasm_trap unreachable``
~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-stack-overflow:

``wasm_trap stack_overflow``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-memory-out-of-bounds:

``wasm_trap memory_out_of_bounds``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-table-out-of-bounds:

``wasm_trap table_out_of_bounds``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-indirect-call-to-null:

``wasm_trap indirect_call_to_null``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-bad-signature:

``wasm_trap bad_signature``
~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-integer-overflow:

``wasm_trap integer_overflow``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-integer-divide-by-zero:

``wasm_trap integer_divide_by_zero``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-bad-conversion-to-integer:

``wasm_trap bad_conversion_to_integer``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-heap-misaligned:

``wasm_trap heap_misaligned``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-atomic-wait-non-shared-memory:

``wasm_trap atomic_wait_non_shared_memory``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-out-of-fuel:

``wasm_trap out_of_fuel``
~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-interrupt:

``wasm_trap interrupt``
~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-nondet-instruction:

``wasm_trap nondet_instruction``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-wasm-trap-fault:

``wasm_trap fault``
~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-memory:

``out_of memory``
~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-memory-wasm-memory:

``out_of memory wasm_memory``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-memory-wasm-table:

``out_of memory wasm_table``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-storage:

``out_of storage``
~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-receipt-nondet-output:

``out_of receipt nondet_output``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-receipt-message:

``out_of receipt message``
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-receipt-event:

``out_of receipt event``
~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-message-fee:

``out_of message_fee``
~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-vm-recursion:

``out_of vm_recursion``
~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-nondet-blocks:

``out_of nondet_blocks``
~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-locked-slots:

``out_of locked_slots``
~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-upgraders:

``out_of upgraders``
~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-out-of-fds:

``out_of fds``
~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-fee-no-matching-node:

``fee no_matching_node``
~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-fee-below-minimal:

``fee below_minimal``
~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-host-forbidden:

``host_forbidden``
~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-evm-reverted:

``evm reverted``
~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract:

``invalid_contract``
~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract-absent-runner-comment:

``invalid_contract absent_runner_comment``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract-not-utf8-text:

``invalid_contract not_utf8_text``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract-malformed-runner:

``invalid_contract malformed_runner``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract-major-mismatch:

``invalid_contract major_mismatch``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract-wasm-validating:

``invalid_contract wasm validating``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract-wasm-linking:

``invalid_contract wasm linking``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-str-trie-value-vm-error-invalid-contract-wasm-entrypoint:

``invalid_contract wasm entrypoint``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. _gvm-def-const-event-max-topics:

event_max_topics
----------------

Type: u32

Value: ``4``
